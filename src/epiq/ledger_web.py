"""FastAPI surface backed exclusively by the authoritative Git ledger."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .document_pipeline import (
    PiChunkExtractor,
    add_document_bytes,
    define_pipeline_spec,
    extract_pipeline,
    prepare_pipeline,
)
from .errors import EpiqError
from .ledger_api import LedgerDatabase
from .ledger_diagnostics import diagnose
from .research import (
    OpenAIResearchRunner,
    PiWorkspaceAgentRunner,
    ResearchRunner,
    WorkspaceAgentRunner,
)
from .temporal import research_instant


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    actor: str = "human:web"


class ProjectOpen(BaseModel):
    project_id: str = Field(min_length=1)


class KindCreate(BaseModel):
    kind: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class EntityCreate(BaseModel):
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    actor: str = "human:web"
    idempotency_key: str | None = None


class QuestionCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    subject_kind: str = Field(min_length=1)
    value_type: str = Field(min_length=1)
    definition: dict[str, Any] = Field(default_factory=dict)
    actor: str = "human:web"
    idempotency_key: str | None = None


class EvidenceCreate(BaseModel):
    source_type: Literal["web", "personal", "model", "report", "interview", "other"] = "web"
    url: str | None = None
    title: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)
    published_at: str | None = None
    excerpt: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class ClaimCreate(BaseModel):
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    value: Any
    valid_from: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"] = "high"
    temporal_basis: Literal["observed", "source", "unknown"] = "observed"
    actor: str = "human:web"
    idempotency_key: str | None = None


class VisibilityCreate(BaseModel):
    reason: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class AssessmentCreate(VisibilityCreate):
    status: Literal["accepted", "disputed", "invalid", "superseded"]


class ReviewCreate(VisibilityCreate):
    decision: Literal["accepted", "rejected"] = "accepted"


class ValidityEndCreate(VisibilityCreate):
    valid_to: str = Field(min_length=1)


class ChallengeCreate(BaseModel):
    problem: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    example_entity: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    proposed_replacement: dict[str, Any] | None = None
    actor: str = "human:web"
    idempotency_key: str | None = None


class ChallengeResolveCreate(BaseModel):
    status: Literal["resolved", "dismissed"]
    resolution: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class NotFoundCreate(BaseModel):
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    query: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class WorkspaceAgentCreate(BaseModel):
    message: str = Field(min_length=1)
    auto_approve: bool = False


class AgentDecisionResponse(BaseModel):
    option: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=4000)


class CuratorCreate(BaseModel):
    objective: str = Field(min_length=1)
    budget_usd: float = Field(gt=0, le=10000)
    max_cycles: int = Field(default=10, ge=1, le=50)
    max_research_cells: int = Field(default=150, ge=0, le=5000)


class CuratorBudgetAdd(BaseModel):
    amount_usd: float = Field(gt=0, le=10000)
    direction: str | None = Field(default=None, min_length=1)


class PipelineDefineCreate(BaseModel):
    spec: dict[str, Any]
    retention: Literal["git", "git-lfs"] = "git"


class PipelinePrepareCreate(BaseModel):
    document_id: str = Field(min_length=1)
    pipeline_id: str = Field(min_length=1)
    retention: Literal["git", "git-lfs"] = "git"


class PipelineExtractCreate(BaseModel):
    run_id: str = Field(min_length=1)
    workers: int = Field(default=4, ge=1, le=8)
    model: str | None = None


def compact_command_observation(argv: list[str], result: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded agent receipt while durable logs retain the lossless result."""

    def compact_text(value: str, limit: int = 500) -> str:
        normalized = " ".join(value.split())
        return normalized if len(normalized) <= limit else f"{normalized[: limit - 3]}..."

    data = result.get("data")
    summary: dict[str, Any] = {}
    if isinstance(data, dict):
        omitted = {"projection", "help", "entries", "cells"}
        for key, value in data.items():
            if key in omitted:
                continue
            if value is None or isinstance(value, (bool, int, float)):
                summary[key] = value
            elif isinstance(value, str):
                summary[key] = compact_text(value)
            elif isinstance(value, list):
                summary[f"{key}_count"] = len(value)
                if key in {
                    "candidates", "entities", "questions", "writes", "notebooks", "sources"
                }:
                    preview_count = min(len(value), 10)
                    source_excerpt_limit = min(
                        4_000,
                        max(500, 12_000 // max(1, preview_count)),
                    )
                    preview = []
                    for item in value[:10]:
                        if not isinstance(item, dict):
                            preview.append(item)
                            continue
                        receipt_fields = [
                            "name", "kind", "entity_id", "question_id", "question_name",
                            "status", "session_id", "objective", "summary", "title", "url",
                            "excerpt",
                        ]
                        # A deliberately opened single page is the retrieval-on-demand path:
                        # give Pi a bounded capture. Multi-result discovery searches remain
                        # manifests so batches cannot flood the persistent conversation.
                        if key == "sources" and len(value) == 1:
                            receipt_fields.append("content")
                        keep = {
                            name: (
                                compact_text(
                                    str(item[name]),
                                    6_000
                                    if key == "sources" and name == "content"
                                    else source_excerpt_limit
                                    if key == "sources" and name == "excerpt"
                                    else 320,
                                )
                                if isinstance(item[name], str)
                                else item[name]
                            )
                            for name in receipt_fields
                            if name in item
                        }
                        preview.append(keep)
                    summary[f"{key}_preview"] = preview
            elif isinstance(value, dict):
                summary[f"{key}_keys"] = list(value)[:20]
    return {
        "type": "command_result",
        "argv": argv,
        "result": {
            "status": result.get("status"),
            "command": result.get("command"),
            "data": summary,
            "errors": result.get("errors", []),
            "warnings": result.get("warnings", []),
            "next_steps": result.get("next_steps", []),
            "full_result_retained_in_timeline": True,
        },
    }


def execute_bounded_command_batch(
    commands: list[list[str]],
    execute: Callable[[list[str]], dict[str, Any]],
    *,
    source_search_workers: int = 4,
) -> dict[str, Any]:
    """Execute mutations in order and consecutive read-heavy searches concurrently."""
    observations: list[dict[str, Any]] = []
    index = 0
    worker_count = max(1, min(8, source_search_workers))
    while index < len(commands):
        command = commands[index]
        if len(command) > 3 and command[3] == "source-search":
            end = index + 1
            while (
                end < len(commands)
                and len(commands[end]) > 3
                and commands[end][3] == "source-search"
            ):
                end += 1
            group = commands[index:end]
            if len(group) == 1 or worker_count == 1:
                observations.append(execute(group[0]))
            else:
                with ThreadPoolExecutor(
                    max_workers=min(worker_count, len(group)),
                    thread_name_prefix="epiq-source-search",
                ) as pool:
                    # executor.map preserves proposed-command order even though
                    # retrieval and the resulting timeline events are concurrent.
                    observations.extend(pool.map(execute, group))
            index = end
        else:
            observations.append(execute(command))
            index += 1

    failed_indexes = [
        position
        for position, observation in enumerate(observations)
        if observation.get("type") == "command_failed"
    ]
    if failed_indexes:
        return {
            "type": "command_batch_failed",
            "failed_index": failed_indexes[0],
            "failed_indexes": failed_indexes,
            "completed_count": len(observations) - len(failed_indexes),
            "results": observations,
        }
    return {
        "type": "command_batch_result",
        "completed_count": len(observations),
        "results": observations,
    }


def create_ledger_app(
    project: str | Path | None = None,
    frontend: str | Path | None = None,
    workspace_agent_runner: WorkspaceAgentRunner | None = None,
    research_runner: ResearchRunner | None = None,
) -> FastAPI:
    """Create the ledger-only local web API."""
    configured_root = Path(
        project or os.environ.get("EPIQ_PROJECT", ".epiq/project")
    ).resolve()
    projects_directory = Path(
        os.environ.get("EPIQ_PROJECTS_DIR", str(configured_root.parent))
    ).resolve()
    active_project_marker = projects_directory / ".epiq-active-project"
    root = configured_root
    if active_project_marker.is_file():
        remembered = Path(active_project_marker.read_text().strip()).resolve()
        if remembered.is_relative_to(projects_directory) and (remembered / "epiq.toml").is_file():
            root = remembered
    checkout_frontend = Path(__file__).parents[2] / "web" / "dist"
    packaged_frontend = Path(__file__).parent / "_frontend"
    frontend_path = Path(
        frontend or (packaged_frontend if packaged_frontend.exists() else checkout_frontend)
    ).resolve()
    app = FastAPI(title="Epiq", version="0.1.0")
    app.state.project = root
    app.state.projects_directory = projects_directory
    app.state.active_project_marker = active_project_marker
    app.state.project_open = root.exists()
    app.state.workspace_agent_runner = workspace_agent_runner or PiWorkspaceAgentRunner(root)
    app.state.workspace_agent_runner_is_default = workspace_agent_runner is None
    app.state.research_runner = research_runner or OpenAIResearchRunner()
    app.state.cli_execution = research_runner is None
    app.state.research_jobs: dict[str, dict[str, Any]] = {}
    app.state.pipeline_jobs: dict[str, dict[str, Any]] = {}
    app.state.emitted_ledger_transactions: dict[str, set[str]] = {}
    app.state.curator: dict[str, Any] | None = None
    app.state.research_lock = threading.RLock()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(EpiqError)
    async def domain_error(_request, error: EpiqError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": error.as_dict()})

    def database() -> LedgerDatabase:
        if not root.exists():
            raise EpiqError(
                "project_not_found",
                "No ledger project has been initialized",
                "POST /api/project with a project name.",
            )
        if not app.state.project_open:
            raise EpiqError(
                "project_not_selected",
                "No project is currently open",
                "Open the configured ledger project.",
            )
        return LedgerDatabase(root)

    def project_directories() -> list[Path]:
        candidates = {root}
        directory = app.state.projects_directory
        if directory.exists():
            candidates.update(
                path.parent for path in directory.glob("*/epiq.toml") if path.is_file()
            )
        return sorted(candidates)

    def select_project(path: Path) -> LedgerDatabase:
        nonlocal root
        selected = LedgerDatabase(path)
        root = path.resolve()
        app.state.project = root
        app.state.project_open = True
        app.state.projects_directory.mkdir(parents=True, exist_ok=True)
        marker = app.state.active_project_marker
        temporary_marker = marker.with_suffix(".tmp")
        temporary_marker.write_text(f"{root}\n")
        temporary_marker.replace(marker)
        if app.state.workspace_agent_runner_is_default:
            app.state.workspace_agent_runner = PiWorkspaceAgentRunner(root)
        with app.state.research_lock:
            app.state.research_jobs.clear()
        hydrate_workspace_history()
        load_curator()
        load_pipeline_jobs()
        return selected

    def project_slug(name: str) -> str:
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        slug = "-".join(part for part in re.split(r"[^a-zA-Z0-9]+", ascii_name.lower()) if part)
        return slug or "epiq-project"

    def ensure_project_switch_safe() -> None:
        with app.state.research_lock:
            active = any(
                job.get("status") in {"queued", "running"}
                or job.get("approval_status") == "applying"
                for job in app.state.research_jobs.values()
            )
            active = active or bool(
                app.state.curator
                and app.state.curator.get("status")
                in {"planning", "awaiting_approval", "running", "stopping"}
            )
            active = active or any(
                job.get("status") in {"queued", "running"}
                for job in app.state.pipeline_jobs.values()
            )
        if active:
            raise EpiqError(
                "research_active",
                "Wait for active Control Center work before switching projects",
            )

    def now() -> str:
        return datetime.now(UTC).isoformat()

    def pipeline_jobs_path() -> Path:
        return root / ".epiq" / "pipeline-jobs.json"

    def save_pipeline_jobs() -> None:
        path = pipeline_jobs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with app.state.research_lock:
            payload = {
                "schema_version": 1,
                "jobs": list(app.state.pipeline_jobs.values()),
            }
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            temporary.replace(path)

    def run_pipeline_job(job_id: str) -> None:
        with app.state.research_lock:
            job = app.state.pipeline_jobs[job_id]
            job.update(
                status="running",
                started_at=job.get("started_at") or now(),
                resumed_at=now() if job.get("attempts", 0) else None,
                attempts=int(job.get("attempts", 0)) + 1,
                error=None,
            )
            run_id = str(job["run_id"])
            workers = int(job.get("workers", 4))
            model = job.get("model")
            completed_indices = {
                int(index) for index in job.get("completed_chunk_indices", [])
            }
        save_pipeline_jobs()

        def progress(index: int, status: str, count: int) -> None:
            event = {
                "at": now(),
                "chunk_index": index,
                "status": status,
                "findings": count,
            }
            with app.state.research_lock:
                current = app.state.pipeline_jobs[job_id]
                current["events"].append(event)
                if status == "completed":
                    completed = set(current.get("completed_chunk_indices", []))
                    completed.add(index)
                    current["completed_chunk_indices"] = sorted(completed)
                    current["chunks_completed"] = len(completed)
                current["findings_written"] = int(current.get("findings_written", 0)) + count
            save_pipeline_jobs()

        try:
            result = extract_pipeline(
                database(),
                run_id,
                PiChunkExtractor(model=str(model) if model else None),
                workers=workers,
                actor=f"agent:pipeline:{job_id}",
                progress=progress,
                skip_chunks=completed_indices,
            )
            with app.state.research_lock:
                job = app.state.pipeline_jobs[job_id]
                job.update(status="completed", result=result, finished_at=now())
        except Exception as error:
            with app.state.research_lock:
                job = app.state.pipeline_jobs[job_id]
                job.update(status="failed", error=str(error), finished_at=now())
        save_pipeline_jobs()

    def start_pipeline_job(job_id: str) -> None:
        threading.Thread(
            target=run_pipeline_job,
            args=(job_id,),
            daemon=True,
            name=f"epiq-pipeline-{job_id}",
        ).start()

    def load_pipeline_jobs() -> None:
        path = pipeline_jobs_path()
        loaded: dict[str, dict[str, Any]] = {}
        if path.is_file():
            try:
                payload = json.loads(path.read_text())
                loaded = {
                    str(item["job_id"]): item
                    for item in payload.get("jobs", [])
                    if isinstance(item, dict) and item.get("job_id")
                }
            except (OSError, json.JSONDecodeError, TypeError):
                loaded = {}
        resumable = []
        for job in loaded.values():
            if job.get("status") in {"queued", "running"}:
                job["status"] = "queued"
                job["recovered_after_restart"] = True
                resumable.append(str(job["job_id"]))
        with app.state.research_lock:
            app.state.pipeline_jobs = loaded
        if loaded:
            save_pipeline_jobs()
        for job_id in resumable:
            start_pipeline_job(job_id)

    def agent_events_path() -> Path:
        return root / ".epiq" / "agent-events.jsonl"

    def emit_agent_event(
        job_id: str,
        event_type: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        actor: str = "epiq",
    ) -> dict[str, Any]:
        event = {
            "event_id": f"evt_{uuid.uuid4().hex[:20]}",
            "job_id": job_id,
            "type": event_type,
            "at": now(),
            "actor": actor,
            "message": message,
            "data": data or {},
        }
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is not None:
                job.setdefault("events", []).append(event)
            path = agent_events_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as stream:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def persisted_agent_events() -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        path = agent_events_path()
        if not path.is_file():
            return grouped
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("job_id"), str):
                grouped.setdefault(event["job_id"], []).append(event)
        return grouped

    def curator_cost_rates() -> tuple[float, float]:
        return (
            float(os.environ.get("EPIQ_CURATOR_PLANNING_COST_USD", "0.75")),
            float(os.environ.get("EPIQ_CURATOR_RESEARCH_CELL_COST_USD", "0.25")),
        )

    def curator_path() -> Path:
        return root / ".epiq" / "curator.json"

    def save_curator() -> None:
        state = app.state.curator
        if state is None or not root.exists():
            return
        path = curator_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)

    def load_curator() -> None:
        path = curator_path()
        if not path.is_file():
            app.state.curator = None
            return
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            app.state.curator = None
            return
        if state.get("status") in {"planning", "running", "stopping"}:
            state["status"] = "paused"
            state["message"] = "Paused after server restart; resume when ready."
        state.setdefault("budget_usd", None)
        state.setdefault("estimated_spend_usd", 0.0)
        state.setdefault("reserved_usd", 0.0)
        state.setdefault("cost_basis", "estimated")
        state.setdefault("failure_streak", 0)
        if state.get("budget_usd") is None and state.get("status") == "paused":
            state.update(
                status="stopped",
                message=(
                    "This earlier curator had no cash budget; create a budgeted curation "
                    "plan to continue."
                ),
                finished_at=now(),
            )
        app.state.curator = state
        save_curator()

    def workspace_context(current: LedgerDatabase) -> dict[str, Any]:
        def bounded_value(value: Any) -> Any:
            if isinstance(value, str) and len(value) > 300:
                return {
                    "value_omitted": True,
                    "characters": len(value),
                    "preview": " ".join(value[:180].split()),
                }
            if isinstance(value, list) and len(value) > 20:
                return {"value_omitted": True, "items": len(value), "sample": value[:5]}
            return value

        def table_context(kind: str) -> dict[str, Any]:
            matrix = current.matrix(kind)
            not_found = {
                (item["subject_id"], item["question_id"])
                for item in current.research_outcomes(kind=kind)
            }
            questions = []
            for question in matrix["questions"]:
                states = {"answered": 0, "not_found": 0, "unasked": 0}
                current_answers = []
                for entity in matrix["entities"]:
                    claims = matrix["cells"].get(entity["entity_id"], {}).get(
                        question["question_id"], []
                    )
                    if claims:
                        states["answered"] += 1
                        if len(current_answers) < 8:
                            current_answers.extend(
                                {
                                    "row": entity["name"],
                                    "claim_id": claim["claim_id"],
                                    "value": bounded_value(claim["value"]),
                                }
                                for claim in claims[: 8 - len(current_answers)]
                            )
                    else:
                        outcome = (entity["entity_id"], question["question_id"]) in not_found
                        states["not_found" if outcome else "unasked"] += 1
                questions.append(
                    {
                        "name": question["name"],
                        "value_type": question["value_type"],
                        "definition": question["definition"],
                        "coverage": states,
                        "answer_sample": current_answers,
                        "answer_sample_limit": 8,
                    }
                )
            row_names = [row["name"] for row in matrix["entities"]]
            return {
                "kind": kind,
                "row_count": len(row_names),
                "rows": row_names[:100],
                "rows_truncated": len(row_names) > 100,
                "questions": questions,
            }
        tables = [table_context(kind) for kind in current.kinds()]
        priorities = []
        for table in tables:
            for question in table["questions"]:
                coverage = question["coverage"]
                unresolved = int(coverage["unasked"]) + int(coverage["not_found"])
                if unresolved == 0:
                    continue
                value_type = str(question["value_type"])
                name = str(question["name"])
                analytical_value = (
                    4.0
                    if value_type.startswith(("Ref[", "Quantity["))
                    or value_type in {"Int", "Float", "Year", "Date", "Bool", "Probability"}
                    or value_type.startswith("Enum[")
                    else 2.5
                    if value_type in {"URL", "Image"}
                    else 1.0
                )
                expected_minutes = (
                    10.0
                    if any(token in name for token in ("transcript", "document_text", "full_text"))
                    else 2.0
                    if value_type in {"URL", "Year", "Date", "Bool"}
                    else 4.0
                )
                priorities.append(
                    {
                        "kind": table["kind"],
                        "question": name,
                        "unresolved_rows": unresolved,
                        "analytical_value": analytical_value,
                        "estimated_minutes_per_batch": expected_minutes,
                        "priority_score": round(
                            unresolved * analytical_value / expected_minutes, 2
                        ),
                        "basis": "heuristic hint; Pi must adjust for user intent and source reuse",
                    }
                )
        priorities.sort(key=lambda item: (-item["priority_score"], item["kind"], item["question"]))
        return {
            "project": current.project(),
            "storage": "git-ledger",
            "control_center_command_contracts": {
                "kind": "kind KIND",
                "entity_batch": "entity-batch KIND --names-json JSON_ARRAY",
                "entity_lookup": "entity-lookup [--kind KIND] [--name EXACT_NAME ...]",
                "ref_batch": (
                    "ref-batch --kind KIND --question FIELD --target-kind KIND "
                    "--items-json JSON_ARRAY; each item has subject, target, source_title, url, "
                    "excerpt, retrieved_at, and valid_from"
                ),
                "question": "question NAME --for KIND --type VALUE_TYPE --definition JSON_OBJECT",
                "source_search": (
                    "source-search --query QUERY [--max-sources 1..10] "
                    "[--include-domain DOMAIN] [--url URL] [--session NOTEBOOK_ID]"
                ),
                "notebook_start": "notebook start --objective TEXT [--scope JSON_OBJECT]",
                "notebook_add": "notebook add SESSION_ID --type TYPE --content JSON_OBJECT",
                "document_add": (
                    "document-add --input FILE [--title TEXT] [--type TYPE] "
                    "[--mime-type MIME] [--source-url URL]"
                ),
                "pipeline_prepare": (
                    "pipeline-prepare --document DOCUMENT_ID --pipeline PIPELINE_ID"
                ),
                "pipeline_define": "pipeline-define --spec-json JSON_OBJECT",
                "pipeline_chunk": "pipeline-chunk --run RUN_ID --index INTEGER",
                "pipeline_apply": "pipeline-apply --run RUN_ID --findings-json JSON_ARRAY",
                "pipeline_extract": (
                    "pipeline-extract --run RUN_ID [--workers 1..8] [--model MODEL]"
                ),
                "record": (
                    "record --subject-id ID --question-id ID --value JSON --valid-from DATE "
                    "--source-title TEXT --source-type web --url URL --retrieved-at DATE "
                    "--excerpt EXACT_QUOTE"
                ),
            },
            "tables": tables,
            "work_priorities": priorities[:20],
            "lab_notebooks": [
                {
                    **{key: value for key, value in notebook.items() if key != "entries"},
                    "entries": notebook.get("entries", [])[-12:],
                }
                for notebook in current.notebooks()[-5:]
            ],
        }

    def curator_metrics(current: LedgerDatabase, strategy: dict[str, Any]) -> dict[str, Any]:
        """Return compact, comparable coverage metrics for campaign reflection."""
        context = workspace_context(current)
        tables = []
        answered = unasked = not_found = 0
        for table in context["tables"]:
            coverage = {"answered": 0, "unasked": 0, "not_found": 0}
            for question in table["questions"]:
                for key in coverage:
                    coverage[key] += int(question["coverage"].get(key, 0))
            answered += coverage["answered"]
            unasked += coverage["unasked"]
            not_found += coverage["not_found"]
            tables.append(
                {
                    "kind": table["kind"],
                    "rows": len(table["rows"]),
                    "fields": len(table["questions"]),
                    **coverage,
                }
            )
        notebooks = context.get("lab_notebooks", [])
        unresolved_leads = sum(
            1
            for notebook in notebooks
            for entry in notebook.get("entries", [])
            if entry.get("entry_type") in {"lead", "source_candidate", "follow_up"}
            or (
                entry.get("entry_type") == "finding"
                and entry.get("content", {}).get("status") == "not_found"
                and entry.get("content", {}).get("notes")
            )
        )
        primary_kind = str(strategy.get("primary_entity_kind") or "")
        primary = next((table for table in tables if table["kind"] == primary_kind), None)
        return {
            "tables": tables,
            "total_rows": sum(table["rows"] for table in tables),
            "answered_cells": answered,
            "unasked_cells": unasked,
            "not_found_cells": not_found,
            "unresolved_leads": unresolved_leads,
            "primary_kind": primary_kind,
            "primary_rows": int(primary["rows"]) if primary else 0,
            "target_primary_rows": int(strategy.get("target_example_count") or 0),
        }

    def curator_phase(metrics: dict[str, Any]) -> str:
        if metrics["primary_rows"] < metrics["target_primary_rows"]:
            return "breadth"
        if metrics["unresolved_leads"]:
            return "follow_up"
        if metrics["unasked_cells"] or metrics["not_found_cells"]:
            return "depth"
        return "expansion"

    def update_job(job_id: str, **changes: Any) -> None:
        with app.state.research_lock:
            app.state.research_jobs[job_id].update(changes)

    def plan_commands(
        plan: dict[str, Any],
        claims_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        if claims_by_id is None:
            claims_by_id = {
                claim["claim_id"]: claim for claim in database().claims(include_closed=True)
            }
        kinds = dict.fromkeys(
            [
                *plan["entity_kinds"],
                *[item["kind"] for item in plan["entities"]],
                *[item["kind"] for item in plan["questions"]],
            ]
        )
        for kind in kinds:
            commands.append(
                {"argv": ["epiq", "--project", str(root), "kind", kind], "status": "proposed"}
            )
        for item in plan["entities"]:
            commands.append(
                {
                    "argv": ["epiq", "--project", str(root), "entity", item["kind"], item["name"]],
                    "status": "proposed",
                }
            )
        for item in plan["questions"]:
            definition = {
                "label": item.get("label") or item["name"].replace("_", " ").title(),
                "cardinality": item.get("cardinality", "one"),
                "volatility": item.get("volatility", "stable"),
                "freshness_days": item.get("freshness_days"),
                "research_guidance": item.get("research_guidance", ""),
            }
            commands.append(
                {
                    "argv": [
                        "epiq",
                        "--project",
                        str(root),
                        "question",
                        item["name"],
                        "--for",
                        item["kind"],
                        "--type",
                        item["value_type"],
                        "--definition",
                        json.dumps(definition, sort_keys=True),
                    ],
                    "status": "proposed",
                }
            )
        for item in plan.get("retractions", []):
            commands.append(
                {
                    "argv": [
                        "epiq",
                        "--project",
                        str(root),
                        "retract-claim",
                        item["claim_id"],
                        "--reason",
                        item["reason"],
                    ],
                    "status": "proposed",
                }
            )
        for item in plan.get("replacements", []):
            claim = claims_by_id.get(item["claim_id"])
            if claim is None:
                raise EpiqError(
                    "claim_not_found",
                    f"Replacement claim not found: {item['claim_id']}",
                )
            argv = [
                "epiq",
                "--project",
                str(root),
                "supersede-claim",
                item["claim_id"],
                "--value",
                json.dumps(item["value"]),
                "--valid-from",
                claim["valid_from"],
            ]
            for evidence_id in claim["evidence_ids"]:
                argv.extend(["--evidence-id", evidence_id])
            argv.extend(
                [
                    "--reason",
                    item["reason"],
                    "--confidence",
                    claim["confidence"],
                    "--temporal-basis",
                    claim["temporal_basis"],
                ]
            )
            commands.append({"argv": argv, "status": "proposed"})
        for item in plan["research"]:
            argv = [
                "epiq",
                "--project",
                str(root),
                "research",
                "--kind",
                item["kind"],
                "--question",
                item["question"],
            ]
            for name in item.get("entity_names", []):
                argv.extend(["--entity-name", name])
            if item.get("instructions"):
                argv.extend(["--instructions", str(item["instructions"])])
            if item.get("allow_estimates"):
                argv.append("--allow-estimates")
            commands.append({"argv": argv, "status": "proposed"})
        return commands

    def transaction_paths() -> dict[str, Path]:
        path = root / "ledger" / "transactions"
        return {
            item.stem: item
            for item in path.glob("*/*.json")
            if item.name.startswith("txn_")
        }

    def ledger_transaction_message(document: dict[str, Any]) -> str | None:
        operations = document.get("operations", [])
        for operation in operations:
            operation_type = operation.get("type")
            payload = operation.get("payload", {})
            if operation_type == "claim.assert":
                try:
                    entity = next(
                        item
                        for item in database().entities()
                        if item["entity_id"] == payload.get("subject_id")
                    )
                    question = next(
                        item
                        for item in database().questions()
                        if item["question_id"] == payload.get("question_id")
                    )
                    return (
                        f"Recorded {question['name']} for {entity['name']}: "
                        f"{json.dumps(payload.get('value'), ensure_ascii=False)}"
                    )
                except (StopIteration, EpiqError):
                    return "Recorded an evidence-backed claim."
            if operation_type == "research.not_found":
                return "Recorded a bounded search with no supported answer."
            if operation_type == "entity.create":
                return f"Added {payload.get('kind')} row: {payload.get('name')}"
            if operation_type == "entity_kind.define":
                return f"Added row type: {payload.get('kind') or payload.get('name')}"
            if operation_type == "question.define":
                return f"Added research field: {payload.get('name')}"
            if operation_type == "notebook.entry_append":
                content = payload.get("content", {})
                if payload.get("entry_type") == "query":
                    sources = content.get("sources", [])
                    return (
                        f"Searched {content.get('query', 'for sources')} · "
                        f"captured {len(sources) if isinstance(sources, list) else 0} sources"
                    )
                if payload.get("entry_type") == "finding":
                    return f"Research finding: {content.get('status', 'recorded')}"
            if operation_type == "evidence.capture":
                return f"Captured evidence: {payload.get('title') or payload.get('url')}"
        return None

    def emit_new_ledger_transactions(
        job_id: str,
        actor: str,
        known: set[str],
    ) -> None:
        for transaction_id, path in sorted(transaction_paths().items()):
            if transaction_id in known:
                continue
            known.add(transaction_id)
            with app.state.research_lock:
                emitted = app.state.emitted_ledger_transactions.setdefault(job_id, set())
                if transaction_id in emitted:
                    continue
                emitted.add(transaction_id)
            try:
                document = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                with app.state.research_lock:
                    emitted.discard(transaction_id)
                continue
            if document.get("actor") != actor:
                with app.state.research_lock:
                    emitted.discard(transaction_id)
                continue
            message = ledger_transaction_message(document)
            if message is None:
                continue
            affected_kinds: set[str] = set()
            affected_rows: list[dict[str, str]] = []
            affected_cells: list[dict[str, str]] = []
            operations = document.get("operations", [])
            try:
                entities_by_id = {
                    str(item["entity_id"]): item for item in database().entities()
                }
                questions_by_id = {
                    str(item["question_id"]): item for item in database().questions()
                }
            except EpiqError:
                entities_by_id = {}
                questions_by_id = {}
            for operation in operations:
                payload = operation.get("payload", {})
                operation_type = operation.get("type")
                if operation_type == "entity.create":
                    kind = str(payload.get("kind") or "")
                    entity_id = str(payload.get("entity_id") or "")
                    if kind:
                        affected_kinds.add(kind)
                    if kind and entity_id:
                        affected_rows.append(
                            {
                                "kind": kind,
                                "entity_id": entity_id,
                                "entity_name": str(payload.get("name") or entity_id),
                            }
                        )
                elif operation_type == "question.define":
                    kind = str(payload.get("subject_kind") or "")
                    if kind:
                        affected_kinds.add(kind)
                elif operation_type in {"claim.assert", "research.not_found"}:
                    entity_id = str(payload.get("subject_id") or "")
                    question_id = str(payload.get("question_id") or "")
                    entity = entities_by_id.get(entity_id, {})
                    question = questions_by_id.get(question_id, {})
                    kind = str(entity.get("kind") or question.get("subject_kind") or "")
                    if kind:
                        affected_kinds.add(kind)
                    if kind and entity_id and question_id:
                        affected_cells.append(
                            {
                                "kind": kind,
                                "entity_id": entity_id,
                                "entity_name": str(entity.get("name") or entity_id),
                                "question_id": question_id,
                                "question_name": str(question.get("name") or question_id),
                            }
                        )
            emit_agent_event(
                job_id,
                "ledger_transaction",
                message,
                data={
                    "transaction_id": document.get("transaction_id"),
                    "recorded_at": document.get("recorded_at"),
                    "actor": document.get("actor"),
                    "operations": operations,
                    "affected_kinds": sorted(affected_kinds),
                    "affected_rows": affected_rows,
                    "affected_cells": affected_cells,
                },
            )

    def execute_cli(argv: list[str], *, job_id: str | None = None) -> dict[str, Any]:
        """Execute one fixed argv command without a shell and return its JSON envelope."""
        if argv[:3] != ["epiq", "--project", str(root)]:
            raise EpiqError("unsafe_command", "Control Center command targets another project")
        actor = f"agent:control-center:{job_id}" if job_id else None
        execution_argv = [*argv[:3]]
        if actor:
            execution_argv.extend(["--actor", actor])
        execution_argv.extend(argv[3:])
        known_transactions = set(transaction_paths())
        if job_id and actor:
            process = subprocess.Popen(
                [sys.executable, "-m", "epiq", *execution_argv[1:]],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    emit_new_ledger_transactions(job_id, actor, known_transactions)
            emit_new_ledger_transactions(job_id, actor, known_transactions)
            completed = subprocess.CompletedProcess(
                execution_argv,
                process.returncode,
                stdout,
                stderr,
            )
        else:
            completed = subprocess.run(
                [sys.executable, "-m", "epiq", *execution_argv[1:]],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
        payload_text = completed.stdout.strip() or completed.stderr.strip()
        if argv[-1:] == ["--help"] and completed.returncode == 0:
            return {
                "schema_version": "1.0",
                "status": "ok",
                "command": "epiq help",
                "argv": argv,
                "data": {"help": payload_text},
                "errors": [],
                "warnings": [],
                "next_steps": ["Use `epiq capabilities` for the machine-readable contract."],
            }
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as error:
            raise EpiqError(
                "invalid_command_result", f"Epiq returned non-JSON output: {payload_text[-500:]}"
            ) from error
        if completed.returncode or payload.get("status") != "ok":
            raise EpiqError(
                "command_failed",
                str(payload.get("errors") or f"Command exited {completed.returncode}"),
            )
        return payload

    def normalize_agent_argv(argv: list[str]) -> list[str]:
        if len(argv) < 4 or argv[0] != "epiq" or argv[1] != "--project":
            raise EpiqError(
                "unsafe_agent_command",
                "Agent commands must start with: epiq --project <current-project>",
            )
        normalized = [str(part) for part in argv]
        if normalized[2] == "PROJECT_PATH":
            normalized[2] = str(root)
        if Path(normalized[2]).resolve() != root:
            raise EpiqError("unsafe_agent_command", "Agent command targets another project")
        if normalized[3:] == ["--help"]:
            return normalized
        allowed = {
            "agent",
            "capabilities",
            "claims",
            "context",
            "contradictions",
            "doctor",
            "discover",
            "source-search",
            "entity",
            "entity-batch",
            "entity-lookup",
            "ref-batch",
            "entity-alias",
            "evidence",
            "gaps",
            "guide",
            "history",
            "kind",
            "matrix",
            "merge-entities",
            "not-found",
            "notebook",
            "document-add",
            "documents",
            "pipeline-define",
            "pipelines",
            "pipeline-prepare",
            "pipeline-chunk",
            "pipeline-apply",
            "pipeline-extract",
            "question",
            "question-challenges",
            "query",
            "record",
            "related",
            "research",
            "retract",
            "retract-claim",
            "schema",
            "search",
            "stale",
            "stale-derivations",
            "supersede",
            "supersede-claim",
            "timeline",
        }
        if normalized[3] not in allowed:
            raise EpiqError(
                "unsafe_agent_command",
                f"Command is not available to the Control Center agent: {normalized[3]}",
            )
        if normalized[3] in {"research", "discover"}:
            raise EpiqError(
                "opaque_research_disallowed",
                "Control Center agents must explore with source-search and persist explicit "
                "rows, evidence, and claims; research/discover are not available here.",
            )
        return normalized

    def command_write_count(result: dict[str, Any]) -> int:
        data = result.get("data")
        if not isinstance(data, dict):
            return 0
        if data.get("created") is True:
            return 1
        if isinstance(data.get("created_count"), int):
            return int(data["created_count"])
        writes = data.get("writes")
        if isinstance(writes, list):
            return sum(
                item.get("status") == "answered"
                for item in writes
                if isinstance(item, dict)
            )
        return int(data.get("written", 0)) if isinstance(data.get("written"), int) else 0

    def hydrate_workspace_history() -> None:
        if not root.exists() or not app.state.project_open:
            return
        current = LedgerDatabase(root)
        claims_by_id = {
            claim["claim_id"]: claim for claim in current.claims(include_closed=True)
        }
        events_by_job = persisted_agent_events()
        app.state.emitted_ledger_transactions = {
            job_id: {
                str(event.get("data", {}).get("transaction_id"))
                for event in events
                if event.get("type") == "ledger_transaction"
                and event.get("data", {}).get("transaction_id")
            }
            for job_id, events in events_by_job.items()
        }
        for proposal in current.proposals():
            if proposal.get("proposal_type") != "workspace_agent":
                continue
            document = proposal.get("document", {})
            plan = document.get("workspace_plan")
            message = document.get("user_message")
            if not isinstance(plan, dict) or not isinstance(message, str):
                continue
            reviews = proposal.get("reviews", [])
            review = reviews[-1] if reviews else None
            decision = review.get("decision") if isinstance(review, dict) else None
            approval = (
                "approved"
                if decision == "accepted"
                else "rejected"
                if decision == "rejected"
                else "pending"
            )
            source_job_id = document.get("source_job_id")
            job_id = (
                str(source_job_id)
                if isinstance(source_job_id, str)
                else f"history_{proposal['proposal_id']}"
            )
            estimated = sum(len(item.get("entity_names", [])) for item in plan["research"])
            app.state.research_jobs[job_id] = {
                "job_id": job_id,
                "job_type": "workspace_agent",
                "entity_kind": "Workspace",
                "question_id": "",
                "mode": "workspace_agent",
                "instructions": message,
                "user_message": message,
                "status": "completed",
                "total": estimated,
                "completed": estimated if approval == "approved" else 0,
                "target_entity_ids": [],
                "error": None,
                "messages": [{"at": "", "message": "Loaded from the project ledger"}],
                "outcome": "workspace_proposal" if approval == "pending" else "changed",
                "assistant_summary": str(plan.get("summary", "")).strip(),
                "workspace_plan": plan,
                "approval_status": approval,
                "estimated_research_cells": estimated,
                "proposal_id": proposal["proposal_id"],
                "command_transcript": plan_commands(plan, claims_by_id),
                "events": events_by_job.get(job_id, []),
                "historical": True,
            }
        for job_id, events in events_by_job.items():
            if job_id in app.state.research_jobs:
                continue
            user_event = next(
                (item for item in events if item.get("type") == "user_message"),
                None,
            )
            if not isinstance(user_event, dict):
                continue
            terminal = next(
                (
                    item
                    for item in reversed(events)
                    if item.get("type")
                    in {"run_completed", "run_failed", "run_cancelled"}
                ),
                None,
            )
            terminal_type = terminal.get("type") if isinstance(terminal, dict) else None
            decision_event = next(
                (
                    item
                    for item in reversed(events)
                    if item.get("type") == "decision_requested"
                ),
                None,
            )
            decision_answered = next(
                (
                    item
                    for item in reversed(events)
                    if item.get("type") == "decision_answered"
                    and isinstance(decision_event, dict)
                    and item.get("at", "") > decision_event.get("at", "")
                ),
                None,
            )
            pending_decision = (
                decision_event.get("data")
                if terminal is None
                and isinstance(decision_event, dict)
                and decision_answered is None
                else None
            )
            status = (
                "waiting_for_user"
                if isinstance(pending_decision, dict)
                else {
                    "run_completed": "completed",
                    "run_failed": "failed",
                    "run_cancelled": "cancelled",
                }.get(str(terminal_type), "failed")
            )
            plan_event = next(
                (
                    item
                    for item in reversed(events)
                    if item.get("type") in {"research_plan", "research_plan_updated"}
                ),
                None,
            )
            summary_event = terminal or next(
                (item for item in reversed(events) if item.get("type") == "agent_message"),
                None,
            )
            command_results = [
                {
                    "argv": item.get("data", {}).get("argv", []),
                    "result": item.get("data", {}).get("result", {}),
                }
                for item in events
                if item.get("type") == "command_result"
            ]
            message = str(user_event.get("message") or "Control Center request")
            app.state.research_jobs[job_id] = {
                "job_id": job_id,
                "job_type": "workspace_agent",
                "entity_kind": "Workspace",
                "question_id": "",
                "mode": "workspace_agent",
                "instructions": message,
                "user_message": message,
                "status": status,
                "total": 0,
                "completed": 0,
                "target_entity_ids": [],
                "created_at": user_event.get("at"),
                "finished_at": terminal.get("at") if isinstance(terminal, dict) else None,
                "error": (
                    str(terminal.get("message"))
                    if terminal_type == "run_failed" and isinstance(terminal, dict)
                    else None
                    if isinstance(pending_decision, dict)
                    else "Run was interrupted by a server restart."
                    if terminal is None
                    else None
                ),
                "messages": [{"at": "", "message": "Loaded from agent event history"}],
                "outcome": "changed" if status == "completed" else "no_change",
                "assistant_summary": (
                    str(summary_event.get("message"))
                    if isinstance(summary_event, dict)
                    else ""
                ),
                "agent_plan": (
                    plan_event.get("data") if isinstance(plan_event, dict) else None
                ),
                "command_results": command_results,
                "events": events,
                "approval_status": (
                    "awaiting_input" if isinstance(pending_decision, dict) else None
                ),
                "pending_decision": pending_decision,
                "auto_approve": bool(user_event.get("data", {}).get("auto_approve")),
                "historical": True,
            }

    hydrate_workspace_history()
    load_curator()
    load_pipeline_jobs()

    def plan_workspace(job_id: str, message: str) -> None:
        def progress(text: str) -> None:
            with app.state.research_lock:
                app.state.research_jobs[job_id]["messages"].append({"at": now(), "message": text})
            emit_agent_event(job_id, "platform_update", text)

        update_job(job_id, status="running", started_at=now())
        emit_agent_event(job_id, "agent_started", "Pi is inspecting the current project.")
        try:
            current = database()
            progress("Thinking through your request")
            context = workspace_context(current)
            plan = app.state.workspace_agent_runner(message, context, progress)
            entities = list(plan["entities"])[:15]
            proposed = {
                (str(item["kind"]), str(item["name"]).casefold()) for item in entities
            }
            existing = {
                (str(table["kind"]), str(name).casefold())
                for table in context["tables"]
                for name in table["rows"]
            }
            new_kinds = {
                str(kind) for kind in plan["entity_kinds"] if str(kind) not in current.kinds()
            }
            # A bounded plan must remain executable after bounding. Preserve rows referenced by
            # research on a newly proposed table even when they fall beyond the display-oriented
            # seed-row limit.
            for request in plan["research"]:
                kind = str(request["kind"])
                for name in request.get("entity_names", []):
                    key = (kind, str(name).casefold())
                    if key not in proposed and key not in existing and kind in new_kinds:
                        entities.append({"kind": kind, "name": str(name)})
                        proposed.add(key)
            plan = {
                **plan,
                "entity_kinds": list(dict.fromkeys(str(x) for x in plan["entity_kinds"]))[:6],
                "entities": entities,
                "questions": list(plan["questions"])[:24],
                "retractions": list(plan.get("retractions", []))[:24],
                "replacements": list(plan.get("replacements", []))[:24],
                "research": list(plan["research"])[:24],
                "decisions": list(plan.get("decisions", []))[:4],
            }
            with app.state.research_lock:
                research_cell_limit = app.state.research_jobs[job_id].get(
                    "research_cell_limit"
                )
            if research_cell_limit is not None:
                remaining = max(0, int(research_cell_limit))
                bounded_research = []
                for request in plan["research"]:
                    names = list(request.get("entity_names", []))[:remaining]
                    if names:
                        bounded_research.append({**request, "entity_names": names})
                        remaining -= len(names)
                    if remaining == 0:
                        break
                plan = {**plan, "research": bounded_research}
            estimated = sum(len(item.get("entity_names", [])) for item in plan["research"])
            with app.state.research_lock:
                auto_approve = bool(app.state.research_jobs[job_id].get("auto_approve"))
            proposal = current.create_proposal(
                {
                    "user_message": message,
                    "workspace_plan": plan,
                    "source_job_id": job_id,
                },
                proposal_type="workspace_agent",
                actor="agent:web",
                idempotency_key=job_id,
            )
            emit_agent_event(
                job_id,
                "agent_message",
                str(plan["summary"]).strip(),
            )
            for command in plan_commands(plan):
                emit_agent_event(
                    job_id,
                    "command_proposed",
                    "Pi proposed an Epiq CLI command.",
                    data={"argv": command["argv"]},
                )
            for decision in plan["decisions"]:
                emit_agent_event(
                    job_id,
                    "decision_requested",
                    str(decision["question"]),
                    data=decision,
                )
            update_job(
                job_id,
                status=(
                    "waiting_for_user"
                    if plan["decisions"]
                    else "queued"
                    if auto_approve
                    else "completed"
                ),
                outcome="workspace_proposal",
                assistant_summary=str(plan["summary"]).strip(),
                workspace_plan=plan,
                approval_status=(
                    "awaiting_input"
                    if plan["decisions"]
                    else "applying"
                    if auto_approve
                    else "pending"
                ),
                estimated_research_cells=estimated,
                proposal_id=proposal["proposal_id"],
                command_transcript=plan_commands(plan),
                finished_at=now(),
            )
            if auto_approve and not plan["decisions"]:
                progress("YOLO mode enabled · applying the proposed CLI plan automatically")
                apply_workspace_plan(job_id)
        except Exception as error:
            update_job(job_id, status="failed", error=str(error), finished_at=now())

    def apply_workspace_plan(job_id: str) -> None:
        with app.state.research_lock:
            job = app.state.research_jobs[job_id]
            plan = job.get("workspace_plan")
        if not plan:
            raise EpiqError("workspace_plan_unavailable", "This workspace plan is unavailable")
        current = database()
        existing_kinds = set(current.kinds())
        existing_entities = {(item["kind"], item["name"].casefold()) for item in current.entities()}
        existing_questions = {(item["subject_kind"], item["name"]) for item in current.questions()}
        written = 0
        command_results: list[dict[str, Any]] = []

        def record_result(argv: list[str], data: Any) -> None:
            command_results.append(
                {
                    "argv": argv,
                    "result": data
                    if isinstance(data, dict) and data.get("schema_version") == "1.0"
                    else {
                        "schema_version": "1.0",
                        "status": "ok",
                        "command": " ".join(argv[:4]),
                        "argv": argv,
                        "data": data,
                        "errors": [],
                        "warnings": [],
                        "next_steps": [],
                    },
                }
            )
            emit_agent_event(
                job_id,
                "command_result",
                "Epiq CLI command completed.",
                data={"argv": argv, "result": command_results[-1]["result"]},
            )

        for kind in dict.fromkeys(
            [
                *plan["entity_kinds"],
                *[item["kind"] for item in plan["entities"]],
                *[item["kind"] for item in plan["questions"]],
            ]
        ):
            if kind not in existing_kinds:
                argv = ["epiq", "--project", str(root), "kind", kind]
                result = (
                    execute_cli(argv)
                    if app.state.cli_execution
                    else current.define_kind(
                        kind, actor="agent:web", idempotency_key=f"{job_id}:kind:{kind}"
                    )
                )
                record_result(argv, result)
                existing_kinds.add(kind)
                written += 1
        for item in plan["entities"]:
            key = (item["kind"], item["name"].casefold())
            if key not in existing_entities:
                argv = ["epiq", "--project", str(root), "entity", item["kind"], item["name"]]
                result = (
                    execute_cli(argv)
                    if app.state.cli_execution
                    else current.create_entity(
                        item["kind"],
                        item["name"],
                        actor="agent:web",
                        idempotency_key=f"{job_id}:entity:{item['kind']}:{item['name']}",
                    )
                )
                record_result(argv, result)
                existing_entities.add(key)
                written += 1
        for item in plan["questions"]:
            key = (item["kind"], item["name"])
            if key not in existing_questions:
                definition = {
                    "label": item.get("label") or item["name"].replace("_", " ").title(),
                    "cardinality": item.get("cardinality", "one"),
                    "volatility": item.get("volatility", "stable"),
                    "freshness_days": item.get("freshness_days"),
                    "research_guidance": item.get("research_guidance", ""),
                }
                argv = [
                    "epiq",
                    "--project",
                    str(root),
                    "question",
                    item["name"],
                    "--for",
                    item["kind"],
                    "--type",
                    item["value_type"],
                    "--definition",
                    json.dumps(definition, sort_keys=True),
                ]
                result = (
                    execute_cli(argv)
                    if app.state.cli_execution
                    else current.define_question(
                        item["name"],
                        item["kind"],
                        item["value_type"],
                        definition=definition,
                        actor="agent:web",
                        idempotency_key=f"{job_id}:question:{item['kind']}:{item['name']}",
                    )
                )
                record_result(argv, result)
                existing_questions.add(key)
                written += 1
        for item in plan.get("retractions", []):
            argv = [
                "epiq",
                "--project",
                str(root),
                "retract-claim",
                item["claim_id"],
                "--reason",
                item["reason"],
            ]
            result = (
                execute_cli(argv)
                if app.state.cli_execution
                else current.retract_claim(
                    item["claim_id"],
                    item["reason"],
                    actor="agent:web",
                    idempotency_key=f"{job_id}:retract:{item['claim_id']}",
                )
            )
            record_result(argv, result)
            written += 1
        claims_by_id = {
            claim["claim_id"]: claim for claim in current.claims(include_closed=True)
        }
        for item in plan.get("replacements", []):
            claim = claims_by_id.get(item["claim_id"])
            if claim is None:
                raise EpiqError(
                    "claim_not_found",
                    f"Replacement claim not found: {item['claim_id']}",
                )
            argv = [
                "epiq",
                "--project",
                str(root),
                "supersede-claim",
                item["claim_id"],
                "--value",
                json.dumps(item["value"]),
                "--valid-from",
                claim["valid_from"],
            ]
            for evidence_id in claim["evidence_ids"]:
                argv.extend(["--evidence-id", evidence_id])
            argv.extend(
                [
                    "--reason",
                    item["reason"],
                    "--confidence",
                    claim["confidence"],
                    "--temporal-basis",
                    claim["temporal_basis"],
                ]
            )
            result = (
                execute_cli(argv)
                if app.state.cli_execution
                else current.supersede_claim(
                    item["claim_id"],
                    item["value"],
                    valid_from=claim["valid_from"],
                    evidence_ids=claim["evidence_ids"],
                    reason=item["reason"],
                    confidence=claim["confidence"],
                    temporal_basis=claim["temporal_basis"],
                    actor="agent:web",
                    idempotency_key=f"{job_id}:replace:{item['claim_id']}",
                )
            )
            record_result(argv, result)
            written += 1
        current.review_proposal(
            job["proposal_id"],
            "accepted",
            "Approved in the workspace agent console",
            actor="human:web",
            idempotency_key=f"{job_id}:approve",
        )
        messages = [
            *job["messages"],
            {
                "at": now(),
                "message": f"Applied {written} schema and row changes.",
            },
        ]
        if plan["research"]:
            update_job(
                job_id,
                status="queued",
                outcome=None,
                approval_status="approved",
                written=written,
                command_results=command_results,
                messages=messages,
            )
            threading.Thread(
                target=execute_workspace_research,
                args=(job_id,),
                daemon=True,
                name=f"epiq-research-{job_id}",
            ).start()
        else:
            update_job(
                job_id,
                status="completed",
                outcome="changed",
                approval_status="approved",
                written=written,
                command_results=command_results,
                finished_at=now(),
                messages=messages,
            )

    def execute_workspace_research(job_id: str) -> None:
        def progress(text: str) -> None:
            with app.state.research_lock:
                app.state.research_jobs[job_id]["messages"].append({"at": now(), "message": text})
            emit_agent_event(job_id, "platform_update", text)

        update_job(job_id, status="running", started_at=now())

        def queue_pi_recovery(argv: list[str], error: Exception) -> str | None:
            with app.state.research_lock:
                parent = app.state.research_jobs[job_id]
                depth = int(parent.get("recovery_depth", 0))
                if depth >= 2:
                    parent["messages"].append(
                        {"at": now(), "message": "Recovery limit reached; human review required"}
                    )
                    return None
                recovery_id = f"recovery_{uuid.uuid4().hex[:16]}"
                command = " ".join(json.dumps(part) for part in argv)
                recovery_message = (
                    "A previous Epiq CLI command failed. Inspect the current schema and the active "
                    "lab notebook, classify the failure, and propose the safest useful next step. "
                    "Do not repeat the same command unchanged. You may repair a value shape, "
                    "refine research instructions, evolve the schema, run a more targeted search, "
                    "reject a "
                    "bad lead, or stop when human judgment is required. Preserve captured evidence "
                    "and existing claims.\n\n"
                    f"Failed command: {command}\n"
                    f"Structured failure: {error}\n"
                    "Original objective: "
                    f"{parent.get('user_message', parent.get('instructions', ''))}"
                )
                recovery = {
                    "job_id": recovery_id,
                    "job_type": "workspace_agent",
                    "entity_kind": "Workspace",
                    "question_id": "",
                    "mode": "workspace_agent",
                    "instructions": recovery_message,
                    "user_message": "Recover from failed research command",
                    "auto_approve": bool(parent.get("auto_approve")),
                    "recovery_depth": depth + 1,
                    "recovery_of_job_id": job_id,
                    "failed_command": argv,
                    "status": "queued",
                    "total": 0,
                    "completed": 0,
                    "target_entity_ids": [],
                    "created_at": now(),
                    "error": None,
                    "messages": [{"at": now(), "message": "Pi recovery queued"}],
                }
                app.state.research_jobs[recovery_id] = recovery
                parent.setdefault("child_job_ids", []).append(recovery_id)
                parent["messages"].append(
                    {"at": now(), "message": f"Passed failure to Pi as {recovery_id}"}
                )
            threading.Thread(
                target=plan_workspace,
                args=(recovery_id, recovery_message),
                daemon=True,
                name=f"epiq-{recovery_id}",
            ).start()
            return recovery_id

        try:
            current = database()
            with app.state.research_lock:
                plan = app.state.research_jobs[job_id]["workspace_plan"]
                schema_writes = int(app.state.research_jobs[job_id].get("written", 0))
                research_commands = [
                    item["argv"]
                    for item in app.state.research_jobs[job_id].get("command_transcript", [])
                    if item["argv"][3:4] == ["research"]
                ]
            if app.state.cli_execution:
                estimated_targets = sum(
                    len(item.get("entity_names", [])) for item in plan["research"]
                )
                update_job(
                    job_id,
                    total=estimated_targets or len(research_commands),
                    completed=0,
                )
                written = 0
                no_result = 0
                for argv in research_commands:
                    with app.state.research_lock:
                        if app.state.research_jobs[job_id].get("cancel_requested"):
                            update_job(job_id, status="cancelled", finished_at=now())
                            return
                    question_name = (
                        argv[argv.index("--question") + 1]
                        if "--question" in argv
                        else "field"
                    )
                    named_targets = argv.count("--entity-name")
                    progress(
                        f"Researching {question_name}"
                        + (f" across {named_targets} rows" if named_targets else "")
                    )
                    emit_agent_event(
                        job_id,
                        "command_started",
                        f"Running research for {question_name}.",
                        data={"argv": argv},
                    )
                    try:
                        result = execute_cli(argv)
                    except Exception as error:
                        recovery_id = queue_pi_recovery(argv, error)
                        if recovery_id is None:
                            raise
                        update_job(
                            job_id,
                            status="failed",
                            error=f"{error} · Pi recovery queued as {recovery_id}",
                            finished_at=now(),
                        )
                        return
                    data = result.get("data", {})
                    writes = data.get("writes", []) if isinstance(data, dict) else []
                    target_count = int(data.get("target_count", len(writes)))
                    written += sum(item.get("status") == "answered" for item in writes)
                    no_result += sum(item.get("status") == "not_found" for item in writes)
                    with app.state.research_lock:
                        app.state.research_jobs[job_id].setdefault("command_results", []).append(
                            {"argv": argv, "result": result}
                        )
                        app.state.research_jobs[job_id]["completed"] += target_count
                        app.state.research_jobs[job_id]["total"] = max(
                            app.state.research_jobs[job_id]["total"],
                            app.state.research_jobs[job_id]["completed"],
                        )
                    emit_agent_event(
                        job_id,
                        "command_result",
                        f"Research command for {question_name} completed.",
                        data={"argv": argv, "result": result},
                    )
                    progress(
                        f"{data.get('kind', 'Research')} · {data.get('question', 'field')}: "
                        f"processed {target_count} rows, wrote {len(writes)} results"
                    )
                progress(f"Research finished: {written} answers, {no_result} without a result")
                update_job(
                    job_id,
                    status="completed",
                    outcome="changed" if schema_writes + written else "no_change",
                    written=schema_writes + written,
                    no_result=no_result,
                    finished_at=now(),
                )
                return
            written = 0
            no_result = 0
            total = sum(len(item.get("entity_names", [])) for item in plan["research"])
            update_job(job_id, total=total, completed=0)
            for request in plan["research"]:
                request_written = 0
                request_no_result = 0
                with app.state.research_lock:
                    if app.state.research_jobs[job_id].get("cancel_requested"):
                        update_job(job_id, status="cancelled", finished_at=now())
                        return
                projection = current.matrix(str(request["kind"]))
                question = next(
                    (
                        item
                        for item in projection["questions"]
                        if item["name"] == str(request["question"])
                    ),
                    None,
                )
                if question is None:
                    progress(f"Skipped missing field {request['kind']}.{request['question']}")
                    continue
                if str(question["value_type"]).startswith("Ref["):
                    field = f"{request['kind']}.{request['question']}"
                    progress(
                        f"Skipped relationship research for {field}; "
                        "relationship proposals are not yet supported by the ledger console"
                    )
                    continue
                requested = {str(name).casefold() for name in request.get("entity_names", [])}
                targets = [
                    {
                        "entity_id": row["entity_id"],
                        "name": row["name"],
                        "attributes": row.get("attributes", {}),
                    }
                    for row in projection["entities"]
                    if not requested or str(row["name"]).casefold() in requested
                ]
                research_question = {
                    **question,
                    "task_mode": "fill_missing",
                    "instructions": str(request.get("instructions", "")),
                    "allow_estimates": bool(request.get("allow_estimates", False)),
                }
                findings = app.state.research_runner(
                    str(request["kind"]), research_question, targets, progress
                )
                targets_by_id = {item["entity_id"]: item for item in targets}
                for finding in findings:
                    with app.state.research_lock:
                        if app.state.research_jobs[job_id].get("cancel_requested"):
                            update_job(job_id, status="cancelled", finished_at=now())
                            return
                    target = targets_by_id.get(str(finding.get("entity_id")))
                    if target is None:
                        continue
                    if finding.get("status") == "not_found":
                        current.record_not_found(
                            target["entity_id"],
                            question["question_id"],
                            f"Agent research for {question['name']}",
                            str(finding.get("notes") or "No sufficient evidence found."),
                            actor="agent:web",
                        )
                        no_result += 1
                        request_no_result += 1
                    elif finding.get("status") == "answered":
                        observed_at = research_instant(finding.get("observed_as_of"))
                        published_at = research_instant(finding.get("source_published_at"))
                        current.record(
                            target["entity_id"],
                            question["question_id"],
                            finding["value"],
                            valid_from=(
                                observed_at
                                or published_at
                                or datetime.now(UTC).date().isoformat()
                            ),
                            source_title=str(finding["source_title"]),
                            source_type=str(finding.get("source_type") or "web"),
                            url=str(finding["source_url"]),
                            retrieved_at=datetime.now(UTC).date().isoformat(),
                            excerpt=str(finding["excerpt"]),
                            published_at=published_at,
                            confidence=str(finding.get("confidence") or "medium"),
                            temporal_basis=(
                                "observed"
                                if finding.get("observed_as_of")
                                else "source"
                                if finding.get("source_published_at")
                                else "unknown"
                            ),
                            epistemic_status=str(
                                finding.get("epistemic_status") or "reported"
                            ),
                            estimate=(
                                finding.get("estimate")
                                if isinstance(finding.get("estimate"), dict)
                                else {}
                            ),
                            actor="agent:web",
                        )
                        written += 1
                        request_written += 1
                    with app.state.research_lock:
                        app.state.research_jobs[job_id]["completed"] += 1
                argv = [
                    "epiq",
                    "--project",
                    str(root),
                    "research",
                    "--kind",
                    str(request["kind"]),
                    "--question",
                    str(request["question"]),
                ]
                for name in request.get("entity_names", []):
                    argv.extend(["--entity-name", str(name)])
                if request.get("instructions"):
                    argv.extend(["--instructions", str(request["instructions"])])
                if request.get("allow_estimates"):
                    argv.append("--allow-estimates")
                with app.state.research_lock:
                    app.state.research_jobs[job_id].setdefault("command_results", []).append(
                        {
                            "argv": argv,
                            "result": {
                                "schema_version": "1.0",
                                "status": "ok",
                                "command": "epiq ledger research",
                                "argv": argv,
                                "data": {
                                    "written": request_written,
                                    "not_found": request_no_result,
                                },
                                "errors": [],
                                "warnings": [],
                                "next_steps": [],
                            },
                        }
                    )
            progress(f"Research finished: {written} answers, {no_result} without a result")
            update_job(
                job_id,
                status="completed",
                outcome="changed" if written or schema_writes else "no_change",
                written=schema_writes + written,
                no_result=no_result,
                finished_at=now(),
            )
        except Exception as error:
            update_job(job_id, status="failed", error=str(error), finished_at=now())

    def execute_agent_action_command(job_id: str, argv: list[str]) -> dict[str, Any]:
        normalized = normalize_agent_argv(argv)
        emit_agent_event(
            job_id,
            "command_started",
            "Running the approved Epiq CLI command.",
            data={"argv": normalized},
        )
        try:
            result = execute_cli(normalized, job_id=job_id)
        except Exception as error:
            emit_agent_event(
                job_id,
                "command_failed",
                str(error),
                data={"argv": normalized, "error": str(error)},
            )
            return {"type": "command_failed", "argv": normalized, "error": str(error)}
        with app.state.research_lock:
            job = app.state.research_jobs[job_id]
            job.setdefault("command_results", []).append(
                {"argv": normalized, "result": result}
            )
            job["written"] = int(job.get("written", 0)) + command_write_count(result)
        emit_agent_event(
            job_id,
            "command_result",
            "Epiq CLI command completed.",
            data={"argv": normalized, "result": result},
        )
        return compact_command_observation(normalized, result)

    def execute_agent_action_commands(
        job_id: str, commands: list[list[str]]
    ) -> dict[str, Any]:
        if len(commands) == 1:
            return execute_agent_action_command(job_id, commands[0])
        return execute_bounded_command_batch(
            commands,
            lambda command: execute_agent_action_command(job_id, command),
            source_search_workers=int(os.environ.get("EPIQ_SOURCE_SEARCH_WORKERS", "4")),
        )

    def finish_gate(job_id: str, completion: Any) -> list[str]:
        with app.state.research_lock:
            job = app.state.research_jobs[job_id]
            plan = job.get("agent_plan")
            cancelled = bool(job.get("cancel_requested"))
        if not isinstance(plan, dict):
            return ["No big-picture research plan was recorded for this objective."]
        if not isinstance(completion, dict):
            return ["Finish must include a completion audit against the research plan."]
        reasons: list[str] = []
        primary_kind = str(plan.get("primary_kind") or "")
        actual_rows = len(database().entities(kind=primary_kind)) if primary_kind else 0
        target_rows = int(plan.get("target_rows") or 0)
        planned_categories = {
            str(item).strip().casefold()
            for item in plan.get("coverage_categories", [])
            if str(item).strip()
        }
        completed_categories = {
            str(item).strip().casefold()
            for item in completion.get("completed_categories", [])
            if str(item).strip()
        }
        missing_categories = planned_categories - completed_categories
        unresolved = int(completion.get("unresolved_leads") or 0)
        stop_reason = str(completion.get("stop_reason") or "")
        blockers = [
            str(item).strip()
            for item in completion.get("blockers", [])
            if str(item).strip()
        ]
        if stop_reason == "completed":
            if actual_rows < target_rows:
                reasons.append(
                    f"The {primary_kind} table has {actual_rows} rows; the plan targets "
                    f"{target_rows}."
                )
            if missing_categories:
                reasons.append(
                    "Coverage categories remain incomplete: "
                    + ", ".join(sorted(missing_categories))
                )
            if unresolved:
                reasons.append(f"The completion audit reports {unresolved} unresolved leads.")
        elif stop_reason in {"budget_exhausted", "tool_blocked"}:
            if not blockers:
                reasons.append(f"{stop_reason} requires at least one concrete blocker.")
        elif stop_reason == "user_stopped":
            if not cancelled:
                reasons.append("The user has not requested that this run stop.")
        else:
            reasons.append("A valid stop reason is required.")
        return reasons

    def run_workspace_agent_loop(
        job_id: str,
        observation: dict[str, Any] | None = None,
    ) -> None:
        def progress(text: str) -> None:
            with app.state.research_lock:
                job = app.state.research_jobs[job_id]
                job["messages"].append({"at": now(), "message": text})
            emit_agent_event(job_id, "platform_update", text)

        update_job(job_id, status="running", started_at=now(), approval_status=None)
        runner = app.state.workspace_agent_runner
        next_action = getattr(runner, "next_action", None)
        if not callable(next_action):
            plan_workspace(job_id, str(app.state.research_jobs[job_id]["instructions"]))
            return
        try:
            while True:
                with app.state.research_lock:
                    job = app.state.research_jobs[job_id]
                    if job.get("cancel_requested"):
                        update_job(job_id, status="cancelled", finished_at=now())
                        emit_agent_event(job_id, "run_cancelled", "Agent run cancelled.")
                        return
                    step = int(job.get("agent_step", 0)) + 1
                    if step > 50:
                        raise RuntimeError("Agent run reached the 50-action safety limit")
                    job["agent_step"] = step
                    goal = str(job.get("instructions") or job.get("user_message") or "")
                context = workspace_context(database()) if step == 1 else None
                action = next_action(goal, context, observation, progress)
                action_type = str(action.get("action", ""))
                message = str(action.get("message", "")).strip()
                argv = action.get("argv") or []
                proposed_commands = action.get("commands") or []
                targets = action.get("targets") or []
                decision = action.get("decision")
                proposed_plan = action.get("plan")
                completion = action.get("completion")
                with app.state.research_lock:
                    app.state.research_jobs[job_id]["active_targets"] = targets
                if isinstance(proposed_plan, dict):
                    with app.state.research_lock:
                        job = app.state.research_jobs[job_id]
                        previous_plan = job.get("agent_plan")
                        job["agent_plan"] = proposed_plan
                    emit_agent_event(
                        job_id,
                        "research_plan" if previous_plan is None else "research_plan_updated",
                        str(proposed_plan.get("objective") or "Research plan updated."),
                        data=proposed_plan,
                    )
                with app.state.research_lock:
                    has_plan = isinstance(
                        app.state.research_jobs[job_id].get("agent_plan"), dict
                    )
                if not has_plan and action_type not in {"decision"}:
                    observation = {
                        "type": "plan_required",
                        "error": (
                            "Record the big-picture plan before messages, commands, or finish."
                        ),
                    }
                    emit_agent_event(
                        job_id,
                        "plan_required",
                        observation["error"],
                        data=observation,
                    )
                    continue
                with app.state.research_lock:
                    history = app.state.research_jobs[job_id].setdefault(
                        "agent_actions", []
                    )
                    if action_type == "message" and history and history[-1].get(
                        "action"
                    ) == "message":
                        observation = {
                            "type": "protocol_error",
                            "error": "Do not emit two message actions in a row.",
                        }
                        continue
                    history.append(action)
                if action_type == "message":
                    emit_agent_event(job_id, "agent_message", message)
                    observation = {"type": "message_delivered"}
                    continue
                if action_type == "command":
                    try:
                        raw_commands = (
                            proposed_commands
                            if proposed_commands
                            else [argv]
                        )
                        if not isinstance(raw_commands, list) or not raw_commands or len(raw_commands) > 20:
                            raise EpiqError(
                                "invalid_agent_batch",
                                "A command action must contain 1–20 Epiq commands",
                            )
                        normalized_commands = [
                            normalize_agent_argv([str(part) for part in command])
                            for command in raw_commands
                            if isinstance(command, list)
                        ]
                        if len(normalized_commands) != len(raw_commands):
                            raise EpiqError(
                                "invalid_agent_batch", "Every batch item must be an argv array"
                            )
                    except EpiqError as error:
                        observation = {
                            "type": "command_rejected",
                            "commands": proposed_commands or [argv],
                            "error": str(error),
                        }
                        emit_agent_event(
                            job_id,
                            "command_rejected",
                            str(error),
                            data=observation,
                        )
                        continue
                    for index, normalized in enumerate(normalized_commands):
                        emit_agent_event(
                            job_id,
                            "command_proposed",
                            message if index == 0 and message else (
                                f"Pi proposed command {index + 1} of {len(normalized_commands)}."
                            ),
                            data={
                                "argv": normalized,
                                "batch_index": index,
                                "batch_size": len(normalized_commands),
                            },
                        )
                    with app.state.research_lock:
                        job = app.state.research_jobs[job_id]
                        job["pending_action"] = {
                            **action,
                            "argv": normalized_commands[0],
                            "commands": normalized_commands,
                        }
                        auto_approve = bool(job.get("auto_approve"))
                    if not auto_approve:
                        update_job(
                            job_id,
                            status="waiting_for_user",
                            approval_status="pending_action",
                        )
                        return
                    observation = execute_agent_action_commands(job_id, normalized_commands)
                    with app.state.research_lock:
                        app.state.research_jobs[job_id].pop("pending_action", None)
                    continue
                if action_type == "decision" and isinstance(decision, dict):
                    emit_agent_event(
                        job_id,
                        "decision_requested",
                        str(decision.get("question") or message),
                        data=decision,
                    )
                    update_job(
                        job_id,
                        status="waiting_for_user",
                        approval_status="awaiting_input",
                        pending_decision=decision,
                    )
                    return
                if action_type == "finish":
                    rejection_reasons = finish_gate(job_id, completion)
                    if rejection_reasons:
                        observation = {
                            "type": "finish_rejected",
                            "reasons": rejection_reasons,
                        }
                        emit_agent_event(
                            job_id,
                            "finish_rejected",
                            "The completion gate found unfinished work.",
                            data=observation,
                        )
                        continue
                    emit_agent_event(job_id, "run_completed", message)
                    update_job(
                        job_id,
                        status="completed",
                        approval_status="approved",
                        assistant_summary=message,
                        completion_audit=completion,
                        active_targets=[],
                        outcome="changed"
                        if int(app.state.research_jobs[job_id].get("written", 0))
                        else "no_change",
                        finished_at=now(),
                    )
                    return
                observation = {
                    "type": "protocol_error",
                    "error": "Return message, command, decision, or finish with valid fields.",
                }
        except Exception as error:
            with app.state.research_lock:
                cancelled = bool(
                    app.state.research_jobs.get(job_id, {}).get("cancel_requested")
                )
            if cancelled:
                emit_agent_event(job_id, "run_cancelled", "Agent run cancelled.")
                update_job(
                    job_id,
                    status="cancelled",
                    error=None,
                    active_targets=[],
                    finished_at=now(),
                )
            else:
                emit_agent_event(job_id, "run_failed", str(error))
                update_job(job_id, status="failed", error=str(error), finished_at=now())

    def continue_approved_agent_action(job_id: str) -> None:
        with app.state.research_lock:
            pending = app.state.research_jobs[job_id].get("pending_action")
        if not isinstance(pending, dict):
            update_job(
                job_id,
                status="failed",
                error="Approved agent action is unavailable",
                finished_at=now(),
            )
            return
        commands = pending.get("commands") or [pending["argv"]]
        observation = execute_agent_action_commands(
            job_id, [[str(part) for part in command] for command in commands]
        )
        with app.state.research_lock:
            app.state.research_jobs[job_id].pop("pending_action", None)
        run_workspace_agent_loop(job_id, observation)

    def run_curator(curator_id: str) -> None:
        while True:
            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                if state.get("stop_requested"):
                    state.update(
                        status="stopped",
                        message="Stopped by the user.",
                        finished_at=now(),
                    )
                    save_curator()
                    return
                if state.get("pause_requested"):
                    state.update(
                        status="paused",
                        message="Paused between enrichment cycles.",
                    )
                    save_curator()
                    return
                cycle = int(state.get("completed_cycles", 0)) + 1
                remaining_cell_cap = int(state["max_research_cells"]) - int(
                    state.get("research_cells_used", 0)
                )
                planning_cost, cell_cost = curator_cost_rates()
                budget = float(state["budget_usd"])
                remaining_usd = budget - float(state.get("estimated_spend_usd", 0.0))
                affordable_cells = max(
                    0,
                    int((remaining_usd - planning_cost) // cell_cost),
                )
                remaining_cells = min(remaining_cell_cap, affordable_cells)
                if (
                    cycle > int(state["max_cycles"])
                    or remaining_cell_cap <= 0
                    or remaining_usd < planning_cost
                ):
                    state.update(
                        status="completed",
                        message="Finished the configured cash budget or safety limits.",
                        finished_at=now(),
                    )
                    save_curator()
                    return
                previous = state.get("cycles", [])[-1] if state.get("cycles") else None
                objective = str(state["objective"])
                strategy = state.get("strategy") or {}
                before_metrics = curator_metrics(database(), strategy)
                phase = curator_phase(before_metrics)
                no_change_streak = int(state.get("no_change_streak", 0))
                fallback_tactics = [
                    "Use direct discovery and authoritative sources.",
                    (
                        "The preceding tactic stalled. Change query vocabulary, source types, "
                        "and discovery routes."
                    ),
                    (
                        "Mine unresolved notebook leads and follow named people, customers, "
                        "products, citations, and publisher archives."
                    ),
                    (
                        "Broaden to adjacent in-scope entities or add a high-value typed "
                        "relationship that unlocks new discovery paths."
                    ),
                ]
                tactic = fallback_tactics[min(no_change_streak, len(fallback_tactics) - 1)]
                continuation_direction = str(
                    state.get("continuation_direction") or ""
                ).strip()
                message = (
                    f"Curator campaign cycle {cycle}. Keep enriching this project toward this "
                    "objective: "
                    f"{objective}\n\nApproved curation strategy: "
                    f"{json.dumps(strategy, sort_keys=True)}\n\n"
                    f"Current campaign phase: {phase}. Live coverage metrics: "
                    f"{json.dumps(before_metrics, sort_keys=True)}\n\n"
                    "Reflect on the current authoritative workspace and execute a substantial "
                    "bounded work package, not a single token enrichment. Use the available "
                    f"capacity of up to {remaining_cells} research cells and up to 15 new rows "
                    "when useful. Prefer useful "
                    "coverage, natural related tables, stronger evidence, and comparison fields. "
                    "Do not repeat completed work or add low-value fields merely to keep going. "
                    "When the objective is a landscape, roster, catalog, or collection, breadth "
                    "comes first: until the primary table has at least 25 credible examples, "
                    "normally add 8–15 new in-scope rows per cycle and research the minimum fields "
                    "needed to verify their identity and relevance. Use later cycles for deeper "
                    "comparison fields and related evidence. Actively revisit promising notebook "
                    "leads, incomplete referenced rows, unasked high-value cells, and prior "
                    "failures with a different evidence strategy. For Ref fields, preserve a "
                    "supported relationship immediately and enrich optional metadata separately. "
                    "Favor "
                    "sortable numerical values with "
                    "units, dates, mutually exclusive categories, narrowly defined yes/no fields, "
                    "URLs, and relationships over long prose. Split compound concepts into atomic "
                    "typed fields; add a String field only when the fact is genuinely textual and "
                    "cannot be represented usefully by a more structured type. "
                    f"Required fallback tactic for this cycle: {tactic}"
                )
                if continuation_direction:
                    message += (
                        "\n\nThe user added this direction when extending the budget: "
                        f"{continuation_direction}. Treat it as the priority within the approved "
                        "strategy for this continuation."
                    )
                if previous:
                    message += (
                        "\n\nPrevious cycle outcome: "
                        f"{previous.get('summary', '')} "
                        f"({previous.get('written', 0)} writes; status "
                        f"{previous.get('status', 'unknown')}). Choose a genuinely useful "
                        "next step."
                    )
                    if previous.get("error"):
                        message += (
                            "\nThe previous cycle failed with this recoverable execution error: "
                            f"{previous['error']}\nDiagnose it, change the command or evidence "
                            "strategy, and continue with work that can succeed. Do not repeat the "
                            "same failing request unchanged."
                        )
                job_id = f"curator_{curator_id}_{cycle}"
                job = {
                    "job_id": job_id,
                    "job_type": "workspace_agent",
                    "entity_kind": "Workspace",
                    "question_id": "",
                    "mode": "workspace_agent",
                    "instructions": message,
                    "user_message": f"Curator cycle {cycle}: {objective}",
                    "auto_approve": True,
                    "curator_id": curator_id,
                    "curator_cycle": cycle,
                    "research_cell_limit": remaining_cells,
                    "estimated_planning_cost_usd": planning_cost,
                    "estimated_research_cell_cost_usd": cell_cost,
                    "status": "queued",
                    "total": 0,
                    "completed": 0,
                    "target_entity_ids": [],
                    "created_at": now(),
                    "error": None,
                    "messages": [{"at": now(), "message": f"Curator cycle {cycle} queued"}],
                }
                app.state.research_jobs[job_id] = job
                state.update(
                    status="running",
                    active_job_id=job_id,
                    reserved_usd=round(planning_cost, 4),
                    current_phase=phase,
                    metrics=before_metrics,
                    message=f"Cycle {cycle} · {phase}: assembling the next work package.",
                )
                save_curator()

            if callable(getattr(app.state.workspace_agent_runner, "next_action", None)):
                run_workspace_agent_loop(job_id)
            else:
                plan_workspace(job_id, message)
            while True:
                with app.state.research_lock:
                    job = app.state.research_jobs[job_id]
                    terminal = job.get("status") in {
                        "completed",
                        "failed",
                        "cancelled",
                        "waiting_for_user",
                    }
                    applying = job.get("approval_status") == "applying"
                if terminal and not applying:
                    break
                time.sleep(0.1)

            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                job = app.state.research_jobs[job_id]
                after_metrics = curator_metrics(database(), strategy)
                attempted = int(job.get("estimated_research_cells", 0))
                cycle_cost = planning_cost + attempted * cell_cost
                cycle_record = {
                    "cycle": cycle,
                    "job_id": job_id,
                    "status": job.get("status"),
                    "summary": job.get("assistant_summary") or job.get("error") or "",
                    "error": job.get("error"),
                    "phase": phase,
                    "written": int(job.get("written", 0)),
                    "research_cells": attempted,
                    "before": before_metrics,
                    "after": after_metrics,
                    "delta": {
                        key: int(after_metrics[key]) - int(before_metrics[key])
                        for key in (
                            "total_rows",
                            "answered_cells",
                            "unasked_cells",
                            "not_found_cells",
                            "unresolved_leads",
                        )
                    },
                    "estimated_cost_usd": round(cycle_cost, 4),
                    "finished_at": now(),
                }
                state.setdefault("cycles", []).append(cycle_record)
                state["completed_cycles"] = cycle
                state["research_cells_used"] = int(
                    state.get("research_cells_used", 0)
                ) + attempted
                state["estimated_spend_usd"] = round(
                    min(
                        float(state["budget_usd"]),
                        float(state.get("estimated_spend_usd", 0.0)) + cycle_cost,
                    ),
                    4,
                )
                state["no_change_streak"] = (
                    0
                    if job.get("status") == "failed" or int(job.get("written", 0)) > 0
                    else int(state.get("no_change_streak", 0)) + 1
                )
                state["active_job_id"] = None
                state["reserved_usd"] = 0.0
                state["metrics"] = after_metrics
                state["current_phase"] = curator_phase(after_metrics)
                state["message"] = str(cycle_record["summary"])
                if job.get("status") == "waiting_for_user":
                    state.update(
                        status="paused",
                        message="The curator found a scope decision that needs your input.",
                    )
                    save_curator()
                    return
                state["failure_streak"] = (
                    int(state.get("failure_streak", 0)) + 1
                    if job.get("status") == "failed"
                    else 0
                )
                if job.get("status") == "failed":
                    state["message"] = (
                        f"Cycle {cycle} failed; the next cycle will diagnose and try a different "
                        f"approach: {job.get('error')}"
                    )
                    if int(state["failure_streak"]) >= 3:
                        state.update(
                            status="paused",
                            message=(
                                "Paused after three consecutive cycle failures. The errors are "
                                "preserved for review; resume after changing configuration or "
                                "giving the curator new direction."
                            ),
                        )
                        save_curator()
                        return
                if int(state["no_change_streak"]) >= 4:
                    state.update(
                        status="completed",
                        message=(
                            "Stopped after four escalating discovery strategies produced no "
                            "useful changes. Add direction or budget to continue."
                        ),
                        finished_at=now(),
                    )
                    save_curator()
                    return
                save_curator()

    def start_curator_thread(curator_id: str) -> None:
        threading.Thread(
            target=run_curator,
            args=(curator_id,),
            daemon=True,
            name=f"epiq-curator-{curator_id}",
        ).start()

    def plan_curator_strategy(curator_id: str) -> None:
        def progress(text: str) -> None:
            with app.state.research_lock:
                state = app.state.curator
                if state and state.get("curator_id") == curator_id:
                    state["message"] = text
                    save_curator()

        try:
            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                objective = str(state["objective"])
                budget_usd = float(state["budget_usd"])
            current = database()
            context = workspace_context(current)
            strategy_method = getattr(
                app.state.workspace_agent_runner, "curator_strategy", None
            )
            if callable(strategy_method):
                strategy = strategy_method(objective, context, budget_usd, progress)
            else:
                primary_kind = (
                    str(context["tables"][0]["kind"])
                    if context.get("tables")
                    else "PrimaryEntity"
                )
                strategy = {
                    "summary": f"Build broad, comparable coverage for {objective}",
                    "primary_entity_kind": primary_kind,
                    "target_example_count": 25,
                    "priority_fields": [],
                    "phases": [
                        {
                            "name": "Breadth",
                            "objective": "Discover and verify in-scope examples.",
                            "target_new_rows": 25,
                            "estimated_research_cells": 0,
                            "budget_usd": max(0.0, budget_usd - curator_cost_rates()[0]),
                        }
                    ],
                    "stopping_criteria": [
                        "Cash budget exhausted",
                        "Two consecutive cycles make no useful changes",
                    ],
                }
            planning_cost, _ = curator_cost_rates()
            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                if state.get("stop_requested"):
                    state.update(
                        status="stopped",
                        reserved_usd=0.0,
                        message="Stopped by the user.",
                        finished_at=now(),
                    )
                    save_curator()
                    return
                state.update(
                    status="awaiting_approval",
                    strategy=strategy,
                    estimated_spend_usd=round(planning_cost, 4),
                    reserved_usd=0.0,
                    message="Review the curation strategy before spending the remaining budget.",
                )
                save_curator()
        except Exception as error:
            with app.state.research_lock:
                state = app.state.curator
                if state and state.get("curator_id") == curator_id:
                    state.update(
                        status="paused",
                        reserved_usd=0.0,
                        message=f"Strategy planning failed: {error}",
                    )
                    save_curator()

    def start_curator_strategy_thread(curator_id: str) -> None:
        threading.Thread(
            target=plan_curator_strategy,
            args=(curator_id,),
            daemon=True,
            name=f"epiq-curator-strategy-{curator_id}",
        ).start()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "project": (
                "ready"
                if root.exists() and app.state.project_open
                else "closed"
                if root.exists()
                else "uninitialized"
            ),
            "path": str(root),
            "initialized": root.exists(),
            "storage": "git-ledger",
        }

    @app.post("/api/project", status_code=201)
    def initialize_project(body: ProjectCreate) -> dict[str, Any]:
        created = LedgerDatabase(root, create=True, name=body.name, actor=body.actor)
        app.state.project_open = True
        return {"project": created.project(), "path": str(root)}

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        ensure_project_switch_safe()
        directory = app.state.projects_directory
        directory.mkdir(parents=True, exist_ok=True)
        slug = project_slug(body.name)
        path = directory / slug
        suffix = 2
        while path.exists():
            path = directory / f"{slug}-{suffix}"
            suffix += 1
        created = LedgerDatabase(path, create=True, name=body.name, actor=body.actor)
        select_project(path)
        project = created.project()
        return {
            "project_id": project["project_id"],
            "name": project["name"],
            "path": str(path),
            "active": True,
        }

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        available = []
        for path in project_directories():
            if not (path / "epiq.toml").exists():
                continue
            project = LedgerDatabase(path).project()
            available.append(
                {
                    "project_id": project["project_id"],
                    "name": project["name"],
                    "path": str(path),
                    "active": path.resolve() == root and bool(app.state.project_open),
                }
            )
        return available

    @app.post("/api/projects/open")
    def open_project(body: ProjectOpen) -> dict[str, Any]:
        selected_path = None
        selected = None
        for path in project_directories():
            if not (path / "epiq.toml").exists():
                continue
            candidate = LedgerDatabase(path)
            if candidate.project()["project_id"] == body.project_id:
                selected_path = path
                selected = candidate
                break
        if selected_path is None or selected is None:
            raise EpiqError("project_not_found", "The selected project is not configured")
        if selected_path.resolve() == root and app.state.project_open:
            project = selected.project()
            return {
                "project_id": project["project_id"],
                "name": project["name"],
                "path": str(selected_path),
                "active": True,
            }
        if selected_path.resolve() != root:
            ensure_project_switch_safe()
        project = select_project(selected_path).project()
        return {
            "project_id": project["project_id"],
            "name": project["name"],
            "path": str(selected_path),
            "active": True,
        }

    @app.post("/api/projects/close")
    def close_project() -> dict[str, bool]:
        with app.state.research_lock:
            active = any(
                job["status"] in {"queued", "running"} for job in app.state.research_jobs.values()
            )
        if active:
            raise EpiqError(
                "research_active", "Wait for active research before closing this project"
            )
        app.state.project_open = False
        return {"closed": True}

    @app.get("/api/project")
    def project_info() -> dict[str, Any]:
        current = database()
        entities = current.entities()
        questions = current.questions()
        return {
            "project": current.project(),
            "entity_kinds": [
                {
                    "kind": kind,
                    "entities": sum(item["kind"] == kind for item in entities),
                    "questions": sum(item["subject_kind"] == kind for item in questions),
                }
                for kind in current.kinds()
            ],
            "storage": "git-ledger",
        }

    @app.post("/api/entity-kinds", status_code=201)
    def create_kind(body: KindCreate) -> dict[str, str]:
        result = database().define_kind(
            body.kind, actor=body.actor, idempotency_key=body.idempotency_key
        )
        return {"kind": result["kind"]}

    @app.post("/api/entities", status_code=201)
    def create_entity(body: EntityCreate) -> dict[str, str]:
        result = database().create_entity(
            body.kind,
            body.name,
            attributes=body.attributes,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"entity_id": result["entity_id"]}

    @app.post("/api/entities/{entity_id}/retire")
    def retire_entity(entity_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_entity_visibility(
            entity_id,
            False,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"entity_id": entity_id, "status": "retired"}

    @app.post("/api/entities/{entity_id}/restore")
    def restore_entity(entity_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_entity_visibility(
            entity_id,
            True,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"entity_id": entity_id, "status": "active"}

    @app.post("/api/questions", status_code=201)
    def create_question(body: QuestionCreate) -> dict[str, str]:
        result = database().define_question(
            body.name,
            body.subject_kind,
            body.value_type,
            definition=body.definition,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"question_id": result["question_id"]}

    @app.post("/api/questions/{question_id}/retire")
    def retire_question(question_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_question_visibility(
            question_id,
            False,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"question_id": question_id, "status": "retired"}

    @app.post("/api/questions/{question_id}/restore")
    def restore_question(question_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_question_visibility(
            question_id,
            True,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"question_id": question_id, "status": "active"}

    @app.post("/api/questions/{question_id}/challenges", status_code=201)
    def challenge_question(question_id: str, body: ChallengeCreate) -> dict[str, str]:
        result = database().challenge_question(
            question_id,
            body.problem,
            body.explanation,
            example_entity_id=body.example_entity,
            evidence_ids=body.evidence_ids,
            proposed_replacement=body.proposed_replacement,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"challenge_id": result["challenge_id"], "status": "open"}

    @app.post("/api/evidence", status_code=201)
    def create_evidence(body: EvidenceCreate) -> dict[str, str]:
        if body.source_type == "web" and not body.url:
            raise EpiqError("source_url_required", "A web source requires a URL")
        result = database().capture_evidence(
            source_title=body.title,
            source_type=body.source_type,
            url=body.url or f"urn:epiq:{body.source_type}",
            retrieved_at=body.retrieved_at,
            published_at=body.published_at,
            excerpt=body.excerpt,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"source_id": result["source_id"], "evidence_id": result["evidence_id"]}

    @app.post("/api/evidence/{evidence_id}/assess", status_code=201)
    def assess_evidence(evidence_id: str, body: AssessmentCreate) -> dict[str, str]:
        result = database().assess_evidence(
            evidence_id,
            body.status,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"assessment_id": result["assessment_id"], "status": body.status}

    @app.post("/api/claims", status_code=201)
    def create_claim(body: ClaimCreate) -> dict[str, str]:
        result = database().assert_claim(
            body.subject,
            body.question,
            body.value,
            valid_from=body.valid_from,
            evidence_ids=body.evidence_ids,
            confidence=body.confidence,
            temporal_basis=body.temporal_basis,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": result["claim_id"]}

    @app.post("/api/claims/{claim_id}/review")
    def review_claim(claim_id: str, body: ReviewCreate) -> dict[str, str]:
        result = database().review_claim(
            claim_id,
            body.decision,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": claim_id, "review_id": result["review_id"]}

    @app.post("/api/claims/{claim_id}/retract")
    def retract_claim(claim_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().retract_claim(
            claim_id,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": claim_id, "status": "retracted"}

    @app.post("/api/claims/{claim_id}/validity-end", status_code=201)
    def end_claim_validity(claim_id: str, body: ValidityEndCreate) -> dict[str, str]:
        database().end_claim_validity(
            claim_id,
            body.valid_to,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": claim_id, "valid_to": body.valid_to}

    @app.get("/api/claims-long")
    def claims(kind: str | None = None, question: str | None = None) -> dict[str, Any]:
        items = database().claims(kind=kind, question=question)
        return {"count": len(items), "claims": items}

    @app.get("/api/matrix/{entity_kind}")
    def matrix(entity_kind: str) -> dict[str, Any]:
        current = database()
        return _web_matrix(current.matrix(entity_kind), current)

    @app.get("/api/related/{entity_id}")
    def related(
        entity_id: str,
        via: str | None = None,
        direction: Literal["incoming", "outgoing", "both"] = "both",
        depth: int = 1,
    ) -> dict[str, Any]:
        if depth < 1 or depth > 20:
            raise EpiqError("invalid_depth", "Relationship depth must be between 1 and 20")
        current = database()
        entities = {item["entity_id"]: item for item in current.entities()}
        target = entities.get(entity_id)
        if target is None:
            raise EpiqError("missing_domain_object", f"Missing entity_id: {entity_id}")
        questions = {
            item["question_id"]: item
            for item in current.questions()
            if str(item["value_type"]).startswith("Ref[") and (via is None or item["name"] == via)
        }
        graph_edges = []
        for claim in current.claims():
            question = questions.get(claim["question_id"])
            if question is None:
                continue
            identifiers = claim["value"] if isinstance(claim["value"], list) else [claim["value"]]
            for identifier in identifiers:
                destination = entities.get(str(identifier))
                source = entities.get(claim["subject_id"])
                if source is None or destination is None:
                    continue
                graph_edges.append(
                    {
                        "question": question["name"],
                        "claim_ids": [claim["claim_id"]],
                        "from": {
                            "entity_id": source["entity_id"],
                            "name": source["name"],
                            "kind": source["kind"],
                        },
                        "to": {
                            "entity_id": destination["entity_id"],
                            "name": destination["name"],
                            "kind": destination["kind"],
                        },
                    }
                )
        edges = []
        frontier = {entity_id: []}
        visited_entities = {entity_id}
        visited_edges: set[tuple[str, str, str, str]] = set()
        for level in range(1, depth + 1):
            next_frontier: dict[str, list[str]] = {}
            for edge in graph_edges:
                candidates = []
                if direction in {"outgoing", "both"} and edge["from"]["entity_id"] in frontier:
                    candidates.append(("outgoing", edge["to"]["entity_id"]))
                if direction in {"incoming", "both"} and edge["to"]["entity_id"] in frontier:
                    candidates.append(("incoming", edge["from"]["entity_id"]))
                for edge_direction, next_id in candidates:
                    key = (
                        edge["from"]["entity_id"],
                        edge["to"]["entity_id"],
                        edge["question"],
                        edge_direction,
                    )
                    if key in visited_edges:
                        continue
                    visited_edges.add(key)
                    current_id = (
                        edge["from"]["entity_id"]
                        if edge_direction == "outgoing"
                        else edge["to"]["entity_id"]
                    )
                    path_claim_ids = [*frontier[current_id], *edge["claim_ids"]]
                    edges.append(
                        {
                            **edge,
                            "direction": edge_direction,
                            "depth": level,
                            "path_claim_ids": path_claim_ids,
                        }
                    )
                    if next_id not in visited_entities:
                        next_frontier[next_id] = path_claim_ids
                        visited_entities.add(next_id)
            frontier = next_frontier
            if not frontier:
                break
        return {
            "entity": {
                "entity_id": target["entity_id"],
                "name": target["name"],
                "kind": target["kind"],
            },
            "via": via,
            "direction": direction,
            "depth": depth,
            "count": len(edges),
            "edges": edges,
        }

    @app.get("/api/conflicts")
    def conflicts() -> dict[str, Any]:
        items = database().conflicts()
        return {"count": len(items), "conflicts": items}

    @app.get("/api/doctor")
    def doctor() -> dict[str, Any]:
        return diagnose(root)

    @app.get("/api/stale/{entity_kind}")
    @app.get("/api/contradictions/{entity_kind}")
    def empty_diagnostics(entity_kind: str) -> dict[str, Any]:
        return {"entity_kind": entity_kind, "count": 0, "cells": []}

    @app.get("/api/stale-derivations")
    def stale_derivations() -> dict[str, Any]:
        return {"count": 0, "stale_derivations": []}

    @app.get("/api/question-challenges")
    def question_challenges(
        question: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        return database().question_challenges(question=question, status=status)

    @app.post("/api/question-challenges/{challenge_id}/resolve")
    def resolve_question_challenge(
        challenge_id: str, body: ChallengeResolveCreate
    ) -> dict[str, str]:
        database().resolve_question_challenge(
            challenge_id,
            body.status,
            body.resolution,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"challenge_id": challenge_id, "status": body.status}

    @app.get("/api/documents")
    def documents() -> list[dict[str, Any]]:
        return [
            item for item in database().reports() if item["report_type"] == "source-document"
        ]

    @app.post("/api/documents", status_code=201)
    async def upload_document(
        request: Request,
        filename: str,
        title: str | None = None,
        document_type: str = "source-document",
        mime_type: str | None = None,
        source_url: str | None = None,
        retention: Literal["git", "git-lfs"] = "git",
    ) -> dict[str, Any]:
        content = await request.body()
        if not content:
            raise EpiqError("empty_document", "Uploaded document is empty")
        if len(content) > 100 * 1024 * 1024:
            raise EpiqError("document_too_large", "Documents may be at most 100 MB")
        return add_document_bytes(
            database(),
            content,
            filename=filename,
            title=title,
            document_type=document_type,
            mime_type=mime_type,
            source_url=source_url,
            retention=retention,
            actor="human:web",
        )

    @app.get("/api/pipelines")
    def pipelines() -> list[dict[str, Any]]:
        return [
            item
            for item in database().reports()
            if item["report_type"] in {"extraction-pipeline", "extraction-run"}
        ]

    @app.post("/api/pipelines", status_code=201)
    def create_pipeline(body: PipelineDefineCreate) -> dict[str, Any]:
        return define_pipeline_spec(
            database(), body.spec, retention=body.retention, actor="human:web"
        )

    @app.post("/api/pipelines/prepare", status_code=201)
    def prepare_document_pipeline(body: PipelinePrepareCreate) -> dict[str, Any]:
        return prepare_pipeline(
            database(),
            body.document_id,
            body.pipeline_id,
            retention=body.retention,
            actor="human:web",
        )

    @app.get("/api/pipeline-jobs")
    def pipeline_jobs() -> list[dict[str, Any]]:
        with app.state.research_lock:
            return list(app.state.pipeline_jobs.values())

    @app.post("/api/pipeline-jobs", status_code=202)
    def launch_pipeline_job(body: PipelineExtractCreate) -> dict[str, Any]:
        job_id = f"pipe_{uuid.uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "run_id": body.run_id,
            "status": "queued",
            "workers": body.workers,
            "model": body.model,
            "chunks_completed": 0,
            "completed_chunk_indices": [],
            "findings_written": 0,
            "attempts": 0,
            "events": [],
            "created_at": now(),
            "error": None,
        }
        with app.state.research_lock:
            app.state.pipeline_jobs[job_id] = job
        save_pipeline_jobs()
        start_pipeline_job(job_id)
        return job

    @app.post("/api/pipeline-jobs/{job_id}/retry", status_code=202)
    def retry_pipeline_job(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.pipeline_jobs.get(job_id)
            if job is None:
                raise EpiqError(
                    "pipeline_job_not_found", f"Pipeline job not found: {job_id}"
                )
            if job.get("status") in {"queued", "running"}:
                raise EpiqError(
                    "pipeline_job_active", "The pipeline job is already active"
                )
            job.update(status="queued", error=None, finished_at=None)
        save_pipeline_jobs()
        start_pipeline_job(job_id)
        return job

    @app.get("/api/research/jobs")
    def research_jobs() -> list[dict[str, Any]]:
        with app.state.research_lock:
            return list(app.state.research_jobs.values())

    @app.get("/api/notebooks")
    def notebooks() -> list[dict[str, Any]]:
        return database().notebooks()

    @app.get("/api/notebooks/{session_id}")
    def notebook(session_id: str) -> dict[str, Any]:
        matches = database().notebooks(session_id=session_id)
        if not matches:
            raise EpiqError(
                "notebook_not_found", f"Research notebook session not found: {session_id}"
            )
        return matches[0]

    @app.get("/api/curator")
    def curator_status() -> dict[str, Any] | None:
        with app.state.research_lock:
            return app.state.curator

    @app.post("/api/curator", status_code=202)
    def launch_curator(body: CuratorCreate) -> dict[str, Any]:
        database().project()
        planning_cost, _ = curator_cost_rates()
        if body.budget_usd < planning_cost:
            raise EpiqError(
                "curator_budget_too_small",
                f"The curation strategy alone is estimated at ${planning_cost:.2f}",
                f"Choose a budget of at least ${planning_cost:.2f}.",
            )
        with app.state.research_lock:
            existing = app.state.curator
            if existing and existing.get("status") in {
                "planning",
                "awaiting_approval",
                "running",
                "stopping",
            }:
                raise EpiqError("curator_active", "A curator is already running for this project")
            curator_id = uuid.uuid4().hex[:16]
            app.state.curator = {
                "curator_id": curator_id,
                "objective": body.objective,
                "budget_usd": body.budget_usd,
                "estimated_spend_usd": 0.0,
                "reserved_usd": planning_cost,
                "cost_basis": "estimated",
                "cost_rates": {
                    "planning_cycle_usd": curator_cost_rates()[0],
                    "research_cell_usd": curator_cost_rates()[1],
                },
                "status": "planning",
                "max_cycles": body.max_cycles,
                "max_research_cells": body.max_research_cells,
                "completed_cycles": 0,
                "research_cells_used": 0,
                "no_change_streak": 0,
                "failure_streak": 0,
                "active_job_id": None,
                "pause_requested": False,
                "stop_requested": False,
                "cycles": [],
                "strategy": None,
                "message": "Pi is designing a curation strategy.",
                "created_at": now(),
            }
            save_curator()
            state = dict(app.state.curator)
        start_curator_strategy_thread(curator_id)
        return state

    @app.post("/api/curator/approve", status_code=202)
    def approve_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") != "awaiting_approval":
                raise EpiqError(
                    "curator_strategy_unavailable",
                    "The curator strategy is not awaiting approval",
                )
            state.update(
                status="running",
                pause_requested=False,
                stop_requested=False,
                message="Starting the first approved enrichment cycle.",
            )
            save_curator()
            curator_id = str(state["curator_id"])
            response = dict(state)
        start_curator_thread(curator_id)
        return response

    @app.post("/api/curator/add-budget", status_code=202)
    def add_curator_budget(body: CuratorBudgetAdd) -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") in {"planning", "running", "stopping"}:
                raise EpiqError(
                    "curator_active",
                    "Wait for the current curator cycle before adding budget",
                )
            if not state.get("strategy") or state.get("budget_usd") is None:
                raise EpiqError(
                    "curator_strategy_unavailable",
                    "Create and approve a budgeted curator strategy first",
                )
            state["budget_usd"] = round(
                float(state["budget_usd"]) + body.amount_usd,
                4,
            )
            if int(state.get("completed_cycles", 0)) >= int(state["max_cycles"]):
                state["max_cycles"] = int(state["max_cycles"]) + 10
            if int(state.get("research_cells_used", 0)) >= int(
                state["max_research_cells"]
            ):
                state["max_research_cells"] = int(state["max_research_cells"]) + 150
            state.update(
                status="running",
                pause_requested=False,
                stop_requested=False,
                no_change_streak=0,
                failure_streak=0,
                message=f"Added ${body.amount_usd:.2f}; continuing the approved strategy.",
            )
            if body.direction:
                state["continuation_direction"] = body.direction.strip()
            state.pop("finished_at", None)
            save_curator()
            curator_id = str(state["curator_id"])
            response = dict(state)
        start_curator_thread(curator_id)
        return response

    @app.post("/api/curator/pause", status_code=202)
    def pause_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") == "running":
                state["pause_requested"] = True
                state["message"] = "Will pause after the current cycle."
                save_curator()
            return dict(state)

    @app.post("/api/curator/resume", status_code=202)
    def resume_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") != "paused":
                raise EpiqError("curator_not_paused", "The curator is not paused")
            state.update(
                status="running",
                pause_requested=False,
                stop_requested=False,
                no_change_streak=0,
                failure_streak=0,
                message="Resuming enrichment.",
            )
            save_curator()
            curator_id = str(state["curator_id"])
            response = dict(state)
        start_curator_thread(curator_id)
        return response

    @app.post("/api/curator/stop", status_code=202)
    def stop_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") in {
                "planning",
                "awaiting_approval",
                "running",
                "paused",
            }:
                state["stop_requested"] = True
                state["pause_requested"] = False
                if state.get("status") in {"paused", "awaiting_approval"}:
                    state.update(status="stopped", finished_at=now())
                else:
                    state["status"] = "stopping"
                state["message"] = "Stopping after the current safe boundary."
                save_curator()
            return dict(state)

    @app.post("/api/workspace-agent/jobs", status_code=202)
    def launch_workspace_agent(body: WorkspaceAgentCreate) -> dict[str, Any]:
        database().project()
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "job_type": "workspace_agent",
            "entity_kind": "Workspace",
            "question_id": "",
            "mode": "workspace_agent",
            "instructions": body.message,
            "user_message": body.message,
            "auto_approve": body.auto_approve,
            "status": "queued",
            "total": 0,
            "completed": 0,
            "target_entity_ids": [],
            "created_at": now(),
            "error": None,
            "messages": [{"at": now(), "message": "Workspace agent queued"}],
            "events": [],
        }
        with app.state.research_lock:
            app.state.research_jobs[job_id] = job
        emit_agent_event(
            job_id,
            "user_message",
            body.message,
            actor="user",
            data={"auto_approve": body.auto_approve},
        )
        threading.Thread(
            target=(
                run_workspace_agent_loop
                if callable(getattr(app.state.workspace_agent_runner, "next_action", None))
                else plan_workspace
            ),
            args=(job_id,) if callable(
                getattr(app.state.workspace_agent_runner, "next_action", None)
            ) else (job_id, body.message),
            daemon=True,
            name=f"epiq-{job_id}",
        ).start()
        return job

    @app.post("/api/workspace-agent/jobs/{job_id}/approve", status_code=202)
    def approve_workspace_agent(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("workspace_job_not_found", f"Workspace job not found: {job_id}")
            if job.get("approval_status") == "pending_action":
                job["approval_status"] = "applying"
                job["status"] = "queued"
                threading.Thread(
                    target=continue_approved_agent_action,
                    args=(job_id,),
                    daemon=True,
                    name=f"epiq-action-{job_id}",
                ).start()
                return job
            if job.get("approval_status") != "pending":
                raise EpiqError(
                    "workspace_plan_unavailable", "This workspace plan is not awaiting approval"
                )
            job["approval_status"] = "applying"
        try:
            apply_workspace_plan(job_id)
        except Exception:
            update_job(job_id, approval_status="pending")
            raise
        with app.state.research_lock:
            return app.state.research_jobs[job_id]

    @app.post("/api/workspace-agent/jobs/{job_id}/reject")
    def reject_workspace_agent(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("workspace_job_not_found", f"Workspace job not found: {job_id}")
            if job.get("approval_status") != "pending":
                raise EpiqError(
                    "workspace_plan_unavailable", "This workspace plan is not awaiting approval"
                )
        database().review_proposal(
            job["proposal_id"],
            "rejected",
            "Dismissed in the workspace agent console",
            actor="human:web",
            idempotency_key=f"{job_id}:reject",
        )
        update_job(job_id, approval_status="rejected", finished_at=now())
        with app.state.research_lock:
            return app.state.research_jobs[job_id]

    @app.post(
        "/api/workspace-agent/jobs/{job_id}/decisions/{decision_id}",
        status_code=202,
    )
    def answer_workspace_decision(
        job_id: str,
        decision_id: str,
        body: AgentDecisionResponse,
    ) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("workspace_job_not_found", f"Workspace job not found: {job_id}")
            if job.get("approval_status") != "awaiting_input":
                raise EpiqError("decision_not_pending", "This agent run is not awaiting a decision")
            pending_decision = job.get("pending_decision")
            if isinstance(pending_decision, dict):
                if pending_decision.get("decision_id") != decision_id:
                    raise EpiqError("decision_not_found", f"Decision not found: {decision_id}")
                options = {
                    str(item["value"]): item for item in pending_decision["options"]
                }
                if body.option not in options:
                    raise EpiqError(
                        "invalid_decision_option", "Choose one of the offered options"
                    )
                selected = options[body.option]
                reason = (body.reason or "").strip()
                observation = {
                    "type": "user_decision",
                    "decision_id": decision_id,
                    "option": body.option,
                    "label": selected["label"],
                    "consequence": selected["consequence"],
                    "reason": reason or None,
                }
                job.pop("pending_decision", None)
                job["decision_response"] = observation
                job["status"] = "queued"
                job["approval_status"] = None
                action_loop = True
            else:
                action_loop = False
        if action_loop:
            emit_agent_event(
                job_id,
                "decision_answered",
                f"Selected {selected['label']}.",
                actor="user",
                data=observation,
            )
            threading.Thread(
                target=run_workspace_agent_loop,
                args=(job_id, observation),
                daemon=True,
                name=f"epiq-decision-{job_id}",
            ).start()
            with app.state.research_lock:
                return app.state.research_jobs[job_id]
        with app.state.research_lock:
            job = app.state.research_jobs[job_id]
            decisions = job.get("workspace_plan", {}).get("decisions", [])
            decision = next(
                (item for item in decisions if item.get("decision_id") == decision_id),
                None,
            )
            if decision is None:
                raise EpiqError("decision_not_found", f"Decision not found: {decision_id}")
            options = {str(item["value"]): item for item in decision["options"]}
            if body.option not in options:
                raise EpiqError("invalid_decision_option", "Choose one of the offered options")
            original_message = str(job.get("user_message") or job.get("instructions") or "")
            auto_approve = bool(job.get("auto_approve"))
        selected = options[body.option]
        reason = (body.reason or "").strip()
        emit_agent_event(
            job_id,
            "decision_answered",
            f"Selected {selected['label']}.",
            actor="user",
            data={
                "decision_id": decision_id,
                "option": body.option,
                "label": selected["label"],
                "reason": reason or None,
            },
        )
        database().review_proposal(
            job["proposal_id"],
            "rejected",
            f"Superseded by user decision {decision_id}: {body.option}"
            + (f" — {reason}" if reason else ""),
            actor="human:web",
            idempotency_key=f"{job_id}:decision:{decision_id}",
        )
        update_job(
            job_id,
            status="completed",
            approval_status="input_received",
            decision_response={
                "decision_id": decision_id,
                "option": body.option,
                "label": selected["label"],
                "reason": reason or None,
            },
            finished_at=now(),
        )
        continuation = (
            f"Continue this project request: {original_message}\n\n"
            f"You asked: {decision['question']}\n"
            f"The user selected: {selected['label']} ({body.option}).\n"
            f"Consequence accepted: {selected['consequence']}\n"
            f"User reason: {reason or 'No additional reason supplied.'}\n\n"
            "Treat this as authoritative project guidance. Continue with visible Epiq CLI actions "
            "and do not ask the same question again."
        )
        next_job = launch_workspace_agent(
            WorkspaceAgentCreate(message=continuation, auto_approve=auto_approve)
        )
        with app.state.research_lock:
            next_job["decision_parent_job_id"] = job_id
            app.state.research_jobs[job_id].setdefault("child_job_ids", []).append(
                next_job["job_id"]
            )
        return next_job

    @app.post("/api/research/jobs/{job_id}/cancel")
    def cancel_research_job(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("research_job_not_found", f"Research job not found: {job_id}")
            job["cancel_requested"] = True
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["finished_at"] = now()
            return job

    @app.post("/api/research/not-found", status_code=201)
    def not_found(body: NotFoundCreate) -> dict[str, str]:
        result = database().record_not_found(
            body.subject,
            body.question,
            body.query,
            body.notes,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"task_id": result["outcome_id"], "state": "NotFound"}

    if frontend_path.exists():
        assets = frontend_path / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            candidate = (frontend_path / path).resolve()
            if not candidate.is_relative_to(frontend_path):
                raise HTTPException(status_code=404, detail="Not found")
            if not candidate.is_file():
                candidate = (frontend_path / "index.html").resolve()
            if not candidate.is_relative_to(frontend_path) or not candidate.is_file():
                raise HTTPException(status_code=404, detail="Not found")
            headers = {"Cache-Control": "no-store"} if candidate.name == "index.html" else None
            return FileResponse(candidate, headers=headers)

    return app


def _web_matrix(projection: dict[str, Any], database: LedgerDatabase) -> dict[str, Any]:
    questions = projection["questions"]
    entities = {item["entity_id"]: item for item in database.entities()}
    derivations = {item["claim_id"]: item for item in database.derivations()}
    outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in database.research_outcomes(kind=projection["kind"]):
        outcomes.setdefault((item["subject_id"], item["question_id"]), []).append(item)
    evidence = {
        item["evidence_id"]: item
        for item in database.evidence(
            evidence_ids=[
                evidence_id
                for entity_cells in projection["cells"].values()
                for claims in entity_cells.values()
                for claim in claims
                for evidence_id in claim["evidence_ids"]
            ]
        )
    }
    rows = []
    for entity in projection["entities"]:
        cells: dict[str, Any] = {}
        for question in questions:
            claims = projection["cells"][entity["entity_id"]][question["question_id"]]
            values = [claim["value"] for claim in claims]
            attempts = outcomes.get((entity["entity_id"], question["question_id"]), [])
            inconclusive_attempts = bool(attempts) and all(
                str(item.get("query") or "").startswith("Agent research")
                for item in attempts
            )
            state = (
                "Answered"
                if len(claims) == 1
                else "Contested"
                if claims
                else "Inconclusive"
                if inconclusive_attempts
                else "NotFound"
                if attempts
                else "Unasked"
            )
            lineage = []
            for claim in claims:
                derivation = derivations.get(claim["claim_id"])
                reviews = claim["reviews"]
                review = reviews[0] if len(reviews) == 1 else None
                for evidence_id in claim["evidence_ids"]:
                    source = evidence[evidence_id]
                    lineage.append(
                        {
                            "token": f"{claim['claim_id']}:{evidence_id}",
                            "claim_id": claim["claim_id"],
                            "value": claim["value"],
                            "confidence": claim["confidence"],
                            "epistemic_status": claim["epistemic_status"],
                            "estimate": claim["estimate"],
                            "evidence_id": evidence_id,
                            "source": {
                                "title": source["title"],
                                "url": source["url"],
                                "published_at": source["published_at"],
                                "retrieved_at": source["retrieved_at"],
                            },
                            "excerpt": source["excerpt"],
                            "as_of": claim["valid_from"],
                            "temporal_basis": claim["temporal_basis"],
                            "review": (
                                {
                                    "status": review["decision"],
                                    "reason": review["reason"],
                                }
                                if review
                                else {"status": "unreviewed"}
                            ),
                            "derivation": (
                                {
                                    "operation": derivation["operation"],
                                    "parameters": derivation["parameters"],
                                    "input_claim_ids": [
                                        claim_id
                                        for values in derivation["inputs"].values()
                                        for claim_id in values
                                    ],
                                    "dependencies": [
                                        {"claim_id": claim_id, "role": role}
                                        for role, claim_ids in derivation["inputs"].items()
                                        for claim_id in claim_ids
                                    ],
                                }
                                if derivation
                                else None
                            ),
                        }
                    )
            cell = {
                "state": state,
                "value": values[0] if len(values) == 1 else None,
                "values": values,
                "claims": claims,
                "lineage": lineage,
                "research": (
                    {
                        "task_id": ",".join(item["outcome_id"] for item in attempts),
                        "query": "\n".join(dict.fromkeys(item["query"] for item in attempts)),
                        "notes": "\n".join(dict.fromkeys(item["notes"] for item in attempts)),
                        "attempts": attempts,
                    }
                    if attempts and not claims
                    else None
                ),
            }
            if str(question["value_type"]).startswith("Ref[") and values:
                identifiers = [
                    identifier
                    for value in values
                    for identifier in (value if isinstance(value, list) else [value])
                ]
                target_kind = str(question["value_type"])[4:-1]
                cell["references"] = [
                    _web_reference(identifier, entities, target_kind) for identifier in identifiers
                ]
            cells[question["name"]] = cell
        rows.append({**entity, "cells": cells})
    return {"entity_kind": projection["kind"], "questions": questions, "rows": rows}


def _web_reference(
    identifier: Any, entities: dict[str, dict[str, Any]], target_kind: str
) -> dict[str, str]:
    """Present resolved IDs and unresolved structured relationship findings safely."""
    if isinstance(identifier, dict):
        entity_id = identifier.get("entity_id")
        entity = entities.get(entity_id, {}) if isinstance(entity_id, str) else {}
        return {
            "entity_id": entity_id or "",
            "kind": str(entity.get("kind") or target_kind),
            "name": str(entity.get("name") or identifier.get("name") or "Unresolved relationship"),
        }
    entity = entities.get(identifier, {})
    return {
        "entity_id": str(identifier),
        "kind": str(entity.get("kind") or target_kind),
        "name": str(entity.get("name", identifier)),
    }
