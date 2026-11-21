import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from epiq import web
from epiq.store import Store
from epiq.web import create_app


def wait_job(client, job_id):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        jobs = client.get("/api/research/jobs").json()
        job = next(item for item in jobs if item["job_id"] == job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        sleep(0.01)
    pytest.fail(f"Job {job_id} did not finish")


@pytest.fixture
def store(tmp_path):
    project = Store(tmp_path / "origin.sqlite")
    project.initialize("Origin")
    project.add_entity("Company", "Acme", {}, "test")
    project.add_entity("Company", "Beta", {}, "test")
    project.add_question("score", "Company", "Int", {}, "test")
    return project


def finding(entity, value=7):
    return {
        "entity_id": entity["entity_id"],
        "status": "answered",
        "value": value,
        "source_url": f"https://example.test/{entity['name']}",
        "source_title": "Scores",
        "excerpt": f"{entity['name']} has score 7",
        "confidence": "high",
        "observed_as_of": "2026-01-01",
    }


def launch_research(client, **overrides):
    return client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Company",
            "question": "q_score_v1",
            **overrides,
        },
    )


def test_recovery_finds_all_unfinished_jobs_beyond_activity_limit(store, tmp_path):
    unfinished = [
        {
            "job_id": f"old_{i}",
            "created_at": "2020-01-01",
            "status": "queued" if i % 2 else "running",
            "cancel_requested": i == 0,
            "messages": [],
        }
        for i in range(1005)
    ]
    recent = [
        {"job_id": f"new_{i}", "created_at": "2026-01-01", "status": "completed"}
        for i in range(220)
    ]
    store.save_agent_jobs([*unfinished, *recent])
    history = store.history()
    client = TestClient(create_app(store.path, tmp_path / "missing"))
    recovered = {job["job_id"]: job for job in client.get("/api/research/jobs").json()}
    assert recovered["old_0"]["status"] == "cancelled"
    assert all(recovered[f"old_{i}"]["status"] == "failed" for i in range(1, 1005))
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM agent_jobs "
                "WHERE json_extract(payload_json,'$.status') IN ('queued','running')"
            ).fetchone()[0]
            == 0
        )
    assert store.history() == history


@pytest.mark.parametrize(
    "job_type,endpoint,payload",
    [
        (
            "research",
            "/api/research/jobs",
            {
                "entity_kind": "Company",
                "question": "q_score_v1",
                "instructions": "Use the report",
                "scope": "row",
            },
        ),
        (
            "entity_suggestions",
            "/api/entity-suggestions/jobs",
            {"entity_kind": "Company", "count": 7, "instructions": "Small firms only"},
        ),
        (
            "field_suggestions",
            "/api/field-suggestions/jobs",
            {"entity_kind": "Company", "count": 9, "instructions": "Financial fields"},
        ),
        (
            "workspace_agent",
            "/api/workspace-agent/jobs",
            {"message": "Prepare a company comparison"},
        ),
    ],
)
def test_retry_after_restart_preserves_each_job_type(store, tmp_path, job_type, endpoint, payload):
    runner_name = {
        "research": "research_runner",
        "entity_suggestions": "suggestion_runner",
        "field_suggestions": "field_suggestion_runner",
        "workspace_agent": "workspace_agent_runner",
    }[job_type]

    def fail(*args):
        raise RuntimeError("temporary runner failure")

    client = TestClient(create_app(store.path, tmp_path / "missing", **{runner_name: fail}))
    response = client.post(endpoint, json=payload)
    assert response.status_code == 202, response.text
    original = wait_job(client, response.json()["job_id"])
    assert original["status"] == "failed"
    calls = []

    def succeed(*args):
        calls.append(args)
        if job_type == "research":
            return [finding(entity) for entity in args[2]]
        if job_type == "workspace_agent":
            return {
                "summary": "No changes needed",
                "entity_kinds": [],
                "entities": [],
                "questions": [],
                "research": [],
            }
        return []

    restarted = TestClient(create_app(store.path, tmp_path / "missing", **{runner_name: succeed}))
    retry = restarted.post(f"/api/research/jobs/{original['job_id']}/retry")
    assert retry.status_code == 202, retry.text
    completed = wait_job(restarted, retry.json()["job_id"])
    assert completed["status"] == "completed", completed.get("error")
    assert completed["request"] == original["request"]
    assert completed["request"]["version"] == 1
    assert completed["request"]["job_type"] == job_type
    assert completed["retry_of"] == original["job_id"]
    assert completed["attempt"] == 2
    assert len(calls) == 1
    if job_type == "entity_suggestions":
        assert calls[0][2:4] == (7, "Small firms only")
    elif job_type == "field_suggestions":
        assert calls[0][3:5] == (9, "Financial fields")
    elif job_type == "workspace_agent":
        assert calls[0][0] == payload["message"]
        assert completed["approval_status"] == "pending"
    else:
        assert calls[0][1]["instructions"] == "Use the report"
    duplicate = restarted.post(f"/api/research/jobs/{original['job_id']}/retry").json()
    assert duplicate["job_id"] == completed["job_id"]
    assert duplicate["deduplicated"]
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["fill_missing", "retry_not_found", "add_evidence"])
def test_retry_skips_committed_findings_and_preserves_target_scope(store, tmp_path, mode):
    if mode == "retry_not_found":
        for name in ("Acme", "Beta"):
            store.record_not_found(name, "score", "initial search", "No evidence", "test")
    elif mode == "add_evidence":
        _, evidence = store.add_evidence(
            "https://initial.test", "Initial", "2026-01-01", "Score 7", "test"
        )
        for name in ("Acme", "Beta"):
            store.assert_claim(name, "score", 7, "2026-01-01", evidence, "test")

    def partial(_kind, _question, entities, progress):
        first = (
            {"entity_id": entities[0]["entity_id"], "status": "not_found", "notes": "Still absent"}
            if mode == "retry_not_found"
            else finding(entities[0])
        )
        return [first, finding(entities[1], "invalid integer")]

    client = TestClient(create_app(store.path, tmp_path / "missing", partial))
    first = launch_research(client, mode=mode).json()
    failed = wait_job(client, first["job_id"])
    assert failed["status"] == "failed"
    assert len(failed["completed_entity_ids"]) == 1
    assert failed["completed"] == 1
    before = store.history()
    store.add_entity("Company", "Gamma", {}, "test")
    called = []

    def remaining(_kind, question, entities, progress):
        called.extend(entity["name"] for entity in entities)
        assert question["task_mode"] == mode
        return [finding(entity) for entity in entities]

    restarted = TestClient(create_app(store.path, tmp_path / "missing", remaining))
    retry = restarted.post(f"/api/research/jobs/{first['job_id']}/retry").json()
    completed = wait_job(restarted, retry["job_id"])
    assert completed["status"] == "completed", completed.get("error")
    assert completed["completed"] == completed["total"] == 2
    assert called == ["Beta"]
    acme = failed["completed_entity_ids"][0]
    assert [e for e in store.history() if e["payload"].get("subject_id") == acme] == [
        e for e in before if e["payload"].get("subject_id") == acme
    ]


def test_failed_checkpoint_rolls_back_all_evidence_and_claims(store, tmp_path, monkeypatch):
    history = store.history()
    save = Store._save_agent_job_tx

    def fail_checkpoint(connection, job):
        if job.get("completed_entity_ids"):
            raise sqlite3.OperationalError("checkpoint write failed")
        save(connection, job)

    monkeypatch.setattr(Store, "_save_agent_job_tx", staticmethod(fail_checkpoint))
    client = TestClient(
        create_app(
            store.path,
            tmp_path / "missing",
            lambda _k, _q, rows, _p: [finding(row) for row in rows],
        )
    )
    job = launch_research(client).json()
    assert wait_job(client, job["job_id"])["status"] == "failed"
    assert store.history() == history
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


def test_process_exit_after_commit_resumes_without_repeating_completed_work(store, tmp_path):
    script = """
import os, sys, time
from fastapi.testclient import TestClient
from epiq.store import Store
from epiq.web import create_app
original = Store.commit_research_result
def commit_then_exit(self, *args, **kwargs):
    original(self, *args, **kwargs)
    os._exit(23)
Store.commit_research_result = commit_then_exit
def runner(kind, question, entities, progress):
    return [dict(entity_id=e['entity_id'], status='answered', value=7,
                 source_url='https://example.test/' + e['name'], source_title='Scores',
                 excerpt='Score 7', confidence='high', observed_as_of='2026-01-01')
            for e in entities]
client = TestClient(create_app(sys.argv[1], sys.argv[2], runner))
client.post('/api/research/jobs', json={'entity_kind':'Company','question':'q_score_v1'})
time.sleep(10)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path), str(tmp_path / "missing")],
        cwd=Path(__file__).resolve().parents[1] / "src",
        timeout=15,
    )
    assert result.returncode == 23
    interrupted = store.agent_jobs()[0]
    assert interrupted["status"] == "running"
    assert len(interrupted["completed_entity_ids"]) == 1
    calls = []

    def runner(_k, _q, rows, _p):
        calls.extend(row["name"] for row in rows)
        return [finding(row) for row in rows]

    restarted = TestClient(create_app(store.path, tmp_path / "missing", runner))
    retry = restarted.post(f"/api/research/jobs/{interrupted['job_id']}/retry").json()
    assert wait_job(restarted, retry["job_id"])["status"] == "completed"
    assert calls == ["Beta"]
    assert len(store.list_claims()) == 2


def test_late_progress_stays_in_its_original_project_after_switch(store, tmp_path):
    callbacks = []

    def runner(_k, _q, rows, progress):
        callbacks.append(progress)
        return [finding(row) for row in rows]

    client = TestClient(
        create_app(
            store.path, tmp_path / "missing", runner, projects_directory=tmp_path / "projects"
        )
    )
    job = launch_research(client).json()
    completed = wait_job(client, job["job_id"])
    response = client.post("/api/projects", json={"name": "Other"})
    assert response.status_code == 201, response.text
    other = Store(response.json()["path"])
    callbacks[0]("Late progress from original runner")
    original = store.agent_job(job["job_id"])
    assert original["project_id"] == completed["project_id"]
    assert original["database"] == str(store.path.resolve())
    assert original["messages"][-1]["message"] == "Late progress from original runner"
    assert other.agent_jobs() == []
    assert client.get("/api/research/jobs").json() == []


def test_project_close_cannot_race_job_admission(store, tmp_path, monkeypatch):
    reading, release_read, running, release_runner = Event(), Event(), Event(), Event()
    original = Store.matrix
    blocked_once = False

    def delayed(self, *args, **kwargs):
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            reading.set()
            assert release_read.wait(3)
        return original(self, *args, **kwargs)

    def runner(_k, _q, rows, _p):
        running.set()
        release_runner.wait(3)
        return [finding(row) for row in rows]

    client = TestClient(create_app(store.path, tmp_path / "missing", runner))
    monkeypatch.setattr(Store, "matrix", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        launched = pool.submit(launch_research, client)
        assert reading.wait(2)
        closing = pool.submit(client.post, "/api/projects/close")
        try:
            release_read.set()
            assert launched.result(timeout=3).status_code == 202
            assert closing.result(timeout=3).status_code == 400
            assert running.wait(2)
        finally:
            release_read.set()
            release_runner.set()


def test_imported_job_metadata_cannot_redirect_recovery_or_retry(store, tmp_path):
    outside = Store(tmp_path / "unrelated.sqlite")
    outside.initialize("Unrelated")
    store.save_agent_job(
        {
            "job_id": "imported",
            "job_type": "research",
            "status": "running",
            "created_at": "2020-01-01",
            "entity_kind": "Company",
            "question_id": "q_score_v1",
            "mode": "fill_missing",
            "database": str(outside.path),
            "project_id": "forged",
            "messages": [],
        }
    )
    client = TestClient(
        create_app(
            store.path,
            tmp_path / "missing",
            lambda _k, _q, rows, _p: [finding(row) for row in rows],
        )
    )
    retry = client.post("/api/research/jobs/imported/retry")
    assert retry.status_code == 202, retry.text
    assert wait_job(client, retry.json()["job_id"])["status"] == "completed"
    assert outside.agent_jobs() == []
    assert outside.history() == []


def test_approved_workspace_retry_reuses_plan_and_already_launched_children(
    store, tmp_path, monkeypatch
):
    plans = []

    def planner(message, context, progress):
        plans.append(message)
        return {
            "summary": "Research company scores",
            "entity_kinds": [],
            "entities": [],
            "questions": [],
            "research": [
                {
                    "kind": "Company",
                    "question": "score",
                    "entity_names": ["Acme", "Beta"],
                    "instructions": "Use the report",
                }
            ],
        }

    calls = []

    def runner(_k, _q, rows, _p):
        calls.extend(row["name"] for row in rows)
        return [finding(row) for row in rows]

    client = TestClient(
        create_app(store.path, tmp_path / "missing", runner, workspace_agent_runner=planner)
    )
    parent = client.post("/api/workspace-agent/jobs", json={"message": "Research scores"}).json()
    assert wait_job(client, parent["job_id"])["approval_status"] == "pending"
    manager = client.app.state.job_manager
    register = manager.register
    launched = 0

    def interrupt_admission(job, payload):
        nonlocal launched
        if job["job_type"] == "research":
            launched += 1
            if launched == 2:
                raise RuntimeError("interrupted after the first child")
        return register(job, payload)

    monkeypatch.setattr(manager, "register", interrupt_admission)
    assert client.post(f"/api/workspace-agent/jobs/{parent['job_id']}/approve").status_code == 202
    failed = wait_job(client, parent["job_id"])
    assert failed["status"] == "failed"
    assert len(failed["child_job_ids"]) == 1
    first_child = failed["child_job_ids"][0]
    assert wait_job(client, first_child)["status"] == "completed"
    restarted = TestClient(
        create_app(store.path, tmp_path / "missing", runner, workspace_agent_runner=planner)
    )
    retry = restarted.post(f"/api/research/jobs/{parent['job_id']}/retry")
    assert retry.status_code == 202, retry.text
    completed = wait_job(restarted, retry.json()["job_id"])
    assert completed["status"] == "completed", completed.get("error")
    assert completed["approval_status"] == "approved"
    assert first_child in completed["child_job_ids"]
    assert len(completed["child_job_ids"]) == 2
    for job_id in completed["child_job_ids"]:
        assert wait_job(restarted, job_id)["status"] == "completed"
    assert plans == ["Research scores"]
    assert sorted(calls) == ["Acme", "Beta"]
    assert len(store.list_claims()) == 2


def test_retry_can_lookup_an_old_failed_job_outside_recent_activity(store, tmp_path):
    store.save_agent_jobs(
        [
            {
                "job_id": "old",
                "job_type": "research",
                "created_at": "2020-01-01",
                "status": "failed",
                "entity_kind": "Company",
                "question_id": "q_score_v1",
                "mode": "fill_missing",
            },
            *[
                {"job_id": f"new_{i}", "created_at": "2026-01-01", "status": "completed"}
                for i in range(220)
            ],
        ]
    )
    client = TestClient(
        create_app(
            store.path,
            tmp_path / "missing",
            lambda _k, _q, rows, _p: [finding(row) for row in rows],
        )
    )
    assert all(job["job_id"] != "old" for job in client.get("/api/research/jobs").json())
    retry = client.post("/api/research/jobs/old/retry")
    assert retry.status_code == 202, retry.text
    assert wait_job(client, retry.json()["job_id"])["status"] == "completed"


def test_legacy_suggestion_without_original_count_has_explicit_retry_error(store, tmp_path):
    store.save_agent_job(
        {
            "job_id": "old",
            "job_type": "field_suggestions",
            "entity_kind": "Company",
            "created_at": "2020-01-01",
            "status": "failed",
        }
    )
    client = TestClient(create_app(store.path, tmp_path / "missing"))
    result = client.post("/api/research/jobs/old/retry")
    assert result.status_code == 400
    assert result.json()["error"]["code"] == "job_request_unavailable"


def test_cancellation_after_partial_completion_keeps_checkpoint_for_retry(
    store, tmp_path, monkeypatch
):
    preparing, release = Event(), Event()
    prepare = web.prepare_finding
    calls = []

    def paused(finding, target, question, mode):
        if target["name"] == "Beta" and not release.is_set():
            preparing.set()
            assert release.wait(3)
        return prepare(finding, target, question, mode)

    monkeypatch.setattr(web, "prepare_finding", paused)

    def runner(_k, _q, rows, _p):
        calls.extend(row["name"] for row in rows)
        return [finding(row) for row in rows]

    client = TestClient(create_app(store.path, tmp_path / "missing", runner))
    original = launch_research(client).json()
    assert preparing.wait(2)
    try:
        assert client.post(f"/api/research/jobs/{original['job_id']}/cancel").status_code == 200
    finally:
        release.set()
    cancelled = wait_job(client, original["job_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["completed"] == 1
    assert len(store.list_claims()) == 1
    retry = client.post(f"/api/research/jobs/{original['job_id']}/retry").json()
    assert wait_job(client, retry["job_id"])["status"] == "completed"
    assert calls == ["Acme", "Beta", "Beta"]
    assert len(store.list_claims()) == 2
