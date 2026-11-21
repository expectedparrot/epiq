"""Project ownership, restart recovery and retry lineage for background jobs."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any

from .errors import EpiqError
from .jobs import JobQueue, QueueFull
from .store import Store


@dataclass
class ProjectJobs:
    store: Store
    project_id: str = ""
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)


def request_envelope(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "job_type": job_type, "payload": deepcopy(payload)}


class JobManager:
    def __init__(self, queue: JobQueue) -> None:
        self.queue = queue
        self.lock = RLock()
        self.active: ProjectJobs | None = None
        self._projects: dict[Path, ProjectJobs] = {}
        self._bound: ContextVar[ProjectJobs | None] = ContextVar("job_project", default=None)
        self._executing: ContextVar[str | None] = ContextVar("executing_job", default=None)
        self._retry: ContextVar[dict[str, Any] | None] = ContextVar("retry_source", default=None)

    @property
    def has_project(self) -> bool:
        return self._bound.get() is not None or self.active is not None

    @property
    def independent_launch(self) -> bool:
        return self._retry.get() is None and self._executing.get() is None

    @property
    def project(self) -> ProjectJobs:
        result = self._bound.get() or self.active
        if result is None:
            raise EpiqError("no_active_project", "No project is currently open")
        return result

    @property
    def jobs(self) -> dict[str, dict[str, Any]]:
        return self.project.jobs

    def activate(self, store: Store | None) -> None:
        """Recover on the first open in this process; retain ownership on later switches."""
        with self.lock:
            if store is None:
                self.active = None
                return
            path = store.path.resolve()
            session = self._projects.get(path)
            if session is None or not session.project_id and path.exists():
                session = ProjectJobs(Store(path))
                if path.exists():
                    session.project_id = session.store.overview()["project"]["project_id"]
                    recovered = session.store.recover_agent_jobs()
                    for job in [*session.store.agent_jobs(), *recovered]:
                        session.jobs[str(job["job_id"])] = self._attach(session, job)
                self._projects[path] = session
            self.active = session

    def _attach(self, session: ProjectJobs, job: dict[str, Any]) -> dict[str, Any]:
        # The opened project owns imported jobs. Never trust an uploaded database's path metadata.
        job["project_id"] = session.project_id
        job["database"] = str(session.store.path)
        if "request" not in job:
            kind = job.get("job_type", "research")
            if kind == "research" and {"entity_kind", "question_id", "mode"} <= job.keys():
                job["request"] = request_envelope(
                    kind,
                    {
                        "entity_kind": job["entity_kind"],
                        "question": job["question_id"],
                        "mode": job["mode"],
                        "instructions": job.get("instructions") or "",
                        "entity_ids": job.get("requested_entity_ids"),
                        "scope": job.get("scope") or "column",
                    },
                )
            elif kind == "workspace_agent" and (job.get("user_message") or job.get("instructions")):
                job["request"] = request_envelope(
                    kind,
                    {
                        "message": job.get("user_message") or job["instructions"],
                    },
                )
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                self.jobs[job_id] = self._attach(self.project, self.project.store.agent_job(job_id))
            return self.jobs[job_id]

    def persist(self, job_id: str) -> None:
        self.project.store.save_agent_job(self.jobs[job_id])

    def persister(self) -> Callable[[str], None]:
        """Capture ownership for progress callbacks, including callbacks on other threads."""
        session = self.project
        return lambda job_id: session.store.save_agent_job(session.jobs[job_id])

    def bound(self, function):
        """Serialize job admission with project switches and capture its project context."""

        @wraps(function)
        def call(*args, **kwargs):
            with self.lock:
                token = self._bound.set(self._bound.get() or self.active)
                try:
                    return function(*args, **kwargs)
                finally:
                    self._bound.reset(token)

        call.__signature__ = inspect.signature(function, eval_str=True)
        return call

    def active_jobs(self) -> bool:
        return self.active is not None and any(
            job.get("status") in {"queued", "running"} for job in self.active.jobs.values()
        )

    def register(self, job: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            session = self.project
            self._attach(session, job)
            job["request"] = request_envelope(job["job_type"], payload)
            source = self._retry.get()
            linked = []
            if source is not None:
                for key in (
                    "completed_entity_ids",
                    "target_entity_ids",
                    "targets_prepared",
                    "completed",
                    "written",
                    "no_result",
                    "rejected",
                    "workspace_plan",
                    "approval_status",
                    "applied",
                    "child_job_ids",
                    "parent_job_id",
                ):
                    if key in source:
                        job[key] = deepcopy(source[key])
                job["retry_of"] = source["job_id"]
                job["retry_root"] = source.get("retry_root", source["job_id"])
                job["attempt"] = int(source.get("attempt", 1)) + 1
                linked.append({**source, "retry_job_id": job["job_id"]})
            parent_id = self._executing.get()
            if parent_id and source is None:
                parent = self.get(parent_id)
                job["parent_job_id"] = parent.get("retry_root", parent_id)
                for child_id in parent.get("child_job_ids", []):
                    child = self.get(child_id)
                    seen = {child_id}
                    while child.get("retry_job_id"):
                        child_id = child["retry_job_id"]
                        if child_id in seen:
                            raise EpiqError("invalid_agent_job", "Retry lineage contains a cycle")
                        seen.add(child_id)
                        child = self.get(child_id)
                    if child.get("request") == job["request"]:
                        return child
                linked.append(
                    {
                        **parent,
                        "child_job_ids": [
                            *parent.get("child_job_ids", []),
                            job["job_id"],
                        ],
                    }
                )
            if source is not None and source.get("parent_job_id"):
                parent = self.get(source["parent_job_id"])
                linked.append(
                    {
                        **parent,
                        "child_job_ids": [
                            job["job_id"] if child == source["job_id"] else child
                            for child in parent.get("child_job_ids", [])
                        ],
                    }
                )
            session.store.save_agent_jobs([job, *linked])
            self.jobs[job["job_id"]] = job
            for record in linked:
                self.jobs[record["job_id"]].update(record)
            return job

    @contextmanager
    def retrying(self, job: dict[str, Any]):
        token = self._retry.set(job)
        try:
            yield
        finally:
            self._retry.reset(token)

    def finish_if_cancelled(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs[job_id]
            if not job.get("cancel_requested"):
                return False
            job["status"] = "cancelled"
            job["finished_at"] = datetime.now(UTC).isoformat()
            self.persist(job_id)
            return True

    def submit(self, target: Callable, job_id: str, body: Any) -> None:
        session = self.project

        def execute() -> None:
            project_token = self._bound.set(session)
            job_token = self._executing.set(job_id)
            try:
                if not self.finish_if_cancelled(job_id):
                    target(job_id, body)
            except Exception as error:
                with self.lock:
                    session.jobs[job_id].update(
                        status="failed",
                        error=str(error),
                        finished_at=datetime.now(UTC).isoformat(),
                    )
                    self.persist(job_id)
                raise
            finally:
                self._executing.reset(job_token)
                self._bound.reset(project_token)

        try:
            self.queue.submit(execute)
        except QueueFull as error:
            with self.lock:
                self.jobs[job_id].update(
                    status="failed",
                    error=str(error),
                    finished_at=datetime.now(UTC).isoformat(),
                )
                self.persist(job_id)
            raise EpiqError("research_queue_full", str(error)) from error
