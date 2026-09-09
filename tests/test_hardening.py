import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from epiq import migrations
from epiq.jobs import JobQueue, QueueFull
from epiq.store import Store
from epiq.web import create_app


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "project.sqlite")
    result.initialize("Hardening")
    return result


@pytest.mark.parametrize("path", ["/%2e%2e/private.txt", "/%2e%2e%2fprivate.txt", "/escape.txt"])
def test_frontend_cannot_serve_outside_its_root(tmp_path, path):
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text("Epiq UI")
    secret = tmp_path / "private.txt"
    secret.write_text("fixture outside the frontend")
    (frontend / "escape.txt").symlink_to(secret)
    client = TestClient(create_app(tmp_path / "project.sqlite", frontend))
    assert client.get(path).status_code == 404
    assert client.get("/nested/client/route").text == "Epiq UI"
    assert client.get("/index.html").text == "Epiq UI"
    (frontend / "index.html").unlink()
    (frontend / "index.html").symlink_to(secret)
    assert client.get("/nested/client/route").status_code == 404


def test_connection_closes_and_rolls_back_on_error(store):
    with pytest.raises(RuntimeError), store.connect() as connection:
        connection.execute("INSERT INTO meta VALUES ('rollback','yes')")
        raise RuntimeError("interrupt")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with store.connect() as connection:
        assert connection.execute("SELECT 1 FROM meta WHERE key='rollback'").fetchone() is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_current_schema_reads_do_not_wait_for_a_writer(store):
    writer = sqlite3.connect(store.path)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            read = executor.submit(store.overview)
            try:
                assert read.result(timeout=2)["project"]["name"] == "Hardening"
            finally:
                writer.rollback()
    finally:
        writer.close()


def legacy_store(tmp_path):
    store = Store(tmp_path / "legacy.sqlite")
    with sqlite3.connect(store.path) as connection:
        connection.executescript((Path(__file__).parent / "fixtures/schema_v1.sql").read_text())
    return store


def test_legacy_migration_is_atomic_and_retryable(tmp_path, monkeypatch):
    store = legacy_store(tmp_path)
    execute = migrations._execute_script
    calls = 0

    def interrupted(connection, script):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted upgrade")
        execute(connection, script)

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "_execute_script", interrupted)
        with pytest.raises(RuntimeError, match="interrupted upgrade"):
            store.connect()
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            == "1"
        )
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='claim_inputs'").fetchone()
            is None
        )
    assert store.migrate()["after"]["current_version"] == migrations.LATEST_SCHEMA_VERSION
    store.add_entity("Company", "Acme", {}, "test")
    store.add_question("score", "Company", "Int", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "Example", "2020-01-01", "Score 5", "test"
    )
    store.assert_claim("Acme", "score", 5, "2020-01-01", evidence, "test")
    assert store.matrix("Company")["rows"][0]["cells"]["score"]["value"] == 5
    assert store.doctor()["ok"]


def test_two_processes_can_upgrade_the_same_legacy_database(tmp_path):
    store = legacy_store(tmp_path)
    script = "from epiq.store import Store; import sys; Store(sys.argv[1]).overview()"
    source = Path(__file__).resolve().parents[1] / "src"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(store.path)],
            cwd=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr.decode()
    assert store.migration_plan()["current_version"] == migrations.LATEST_SCHEMA_VERSION


def test_historical_matrix_preserves_schema_identity_and_research(store):
    entity = store.add_entity("Company", "Acme", {}, "test")
    duplicate = store.add_entity("Company", "Acme duplicate", {}, "test")
    store.add_question("score", "Company", "Int", {}, "test")
    store.add_entity_alias(entity, "Original alias", "test")
    cutoff = store.history()[-1]["recorded_at"]
    before = store.matrix("Company", known_at=cutoff)
    store.record_not_found(entity, "score", "search", "nothing found", "test")
    store.add_entity_alias(duplicate, "Later alias", "test")
    store.merge_entities(duplicate, entity, "duplicate", "test")
    store.add_question("score", "Company", "String", {"label": "New version"}, "test")
    store.set_question_visibility("score", False, "retire", "test")
    store.set_entity_visibility(entity, False, "retire", "test")
    retired_at = store.history()[-1]["recorded_at"]
    retired = store.matrix("Company", known_at=retired_at)
    store.set_entity_visibility(entity, True, "restore", "test")
    store.set_question_visibility("score", True, "restore", "test")
    store.add_entity("Company", "Future row", {}, "test")
    store.add_question("future_field", "Company", "Bool", {}, "test")
    assert store.matrix("Company", known_at=cutoff) == before
    assert store.matrix("Company", known_at=retired_at) == retired
    assert len(before["rows"]) == 2
    assert before["questions"][0]["value_type"] == "Int"
    assert retired["rows"] == []
    assert store.matrix("Company")["rows"][0]["cells"]["score"]["state"] == "NotFound"


def test_projection_query_count_is_bounded_for_empty_and_answered_cells(store):
    store.apply_document(
        {
            "entities": [{"kind": "Company", "name": f"Company {i}"} for i in range(100)],
            "questions": [
                {"name": f"score_{i}", "subject_kind": "Company", "value_type": "Int"}
                for i in range(10)
            ],
        },
        "test",
    )
    statements = []

    class TracedStore(Store):
        def connect(self):
            connection = super().connect()
            connection.set_trace_callback(statements.append)
            return connection

    traced = TracedStore(store.path)
    table = traced.matrix("Company")
    assert len(table["rows"]) == 100
    assert len(table["questions"]) == 10
    assert len(statements) <= 15
    _, evidence = store.add_evidence(
        "https://example.test", "Scores", "2020-01-01", "Score 5", "test"
    )
    store.assert_claims_bulk(
        [
            {
                "subject": row["entity_id"],
                "question": "score_0",
                "value": 5,
                "valid_from": "2020-01-01",
                "evidence_ids": [evidence],
            }
            for row in table["rows"]
        ],
        "test",
    )
    statements.clear()
    answered = traced.matrix("Company")
    assert all(row["cells"]["score_0"]["value"] == 5 for row in answered["rows"])
    assert len(statements) <= 20


def test_historical_references_challenges_and_assessments_do_not_change(store):
    author = store.add_entity("Person", "Ada", {}, "test")
    survivor = store.add_entity("Person", "Ada Lovelace", {}, "test")
    work = store.add_entity("Work", "Notes", {}, "test")
    question = store.add_question("author", "Work", "Ref[Person]", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "Notes", "2020-01-01", "Written by Ada", "test"
    )
    store.assert_claim(work, question, author, "2020-01-01", evidence, "test")
    challenge = store.challenge_question(question, "definition_ambiguity", "Which author?", "test")
    cutoff = store.history()[-1]["recorded_at"]
    before = store.matrix("Work", known_at=cutoff)
    store.merge_entities(author, survivor, "Same person", "test")
    store.assess_evidence(evidence, "disputed", "Needs review", "test")
    store.resolve_question_challenge(challenge, "resolved", "Reviewed", "test")
    store.evolve_question(
        question, [{"name": "writer", "value_type": "Ref[Person]"}], "replaces", "Clarify", "test"
    )
    assert store.matrix("Work", known_at=cutoff) == before
    cell = before["rows"][0]["cells"]["author"]
    assert cell["display_value"]["name"] == "Ada"
    assert cell["lineage"][0]["evidence_status"] == "unassessed"
    assert before["questions"][0]["schema_state"] == "challenged"
    assert [q["name"] for q in store.matrix("Work")["questions"]] == ["writer"]


def test_matrix_uses_one_read_snapshot_during_concurrent_writes(store):
    entity = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("score", "Company", "Int", {}, "test")
    changed = False

    def trace(statement):
        nonlocal changed
        if not changed and "FROM research_tasks" in statement:
            changed = True
            store.record_not_found(entity, "score", "search", "No result", "test")

    class SnapshotStore(Store):
        def connect(self):
            connection = super().connect()
            connection.set_trace_callback(trace)
            return connection

    during_write = SnapshotStore(store.path).matrix("Company")
    assert changed
    assert during_write["rows"][0]["cells"]["score"]["state"] == "Unasked"
    assert store.matrix("Company")["rows"][0]["cells"]["score"]["state"] == "NotFound"


def test_job_queue_bounds_work_and_recovers_after_a_task_failure():
    queue = JobQueue(workers=1, pending=1)
    started, release, completed = Event(), Event(), Event()

    def first():
        started.set()
        release.wait(timeout=3)
        raise RuntimeError("failed task")

    queue.submit(first)
    assert started.wait(timeout=2)
    try:
        queue.submit(completed.set)
        with pytest.raises(QueueFull):
            queue.submit(lambda: None)
        assert not completed.is_set()
    finally:
        release.set()
    assert completed.wait(timeout=2)


def test_cancelled_queued_research_never_calls_runner(store, tmp_path, monkeypatch):
    monkeypatch.setenv("EPIQ_RESEARCH_CONCURRENCY", "1")
    monkeypatch.setenv("EPIQ_RESEARCH_QUEUE_CAPACITY", "1")
    entities = [store.add_entity("Company", name, {}, "test") for name in ("Acme", "Beta", "Gamma")]
    question = store.add_question("score", "Company", "Int", {}, "test")
    started, release, cancelled = Event(), Event(), Event()
    calls = []

    def runner(_kind, _question, targets, progress=None):
        calls.extend(item["entity_id"] for item in targets)
        started.set()
        release.wait(timeout=3)
        return []

    app = create_app(store.path, tmp_path / "missing", runner)
    client = TestClient(app)

    def launch(entity):
        return client.post(
            "/api/research/jobs",
            json={
                "entity_kind": "Company",
                "question": question,
                "entity_ids": [entity],
            },
        )

    assert launch(entities[0]).status_code == 202
    assert started.wait(timeout=2)
    try:
        waiting = launch(entities[1]).json()
        assert waiting["status"] == "queued"
        assert launch(entities[2]).status_code == 429
        assert client.post(f"/api/research/jobs/{waiting['job_id']}/cancel").status_code == 200
        save = Store.save_agent_job

        def track(self, job):
            save(self, job)
            if job["job_id"] == waiting["job_id"] and job["status"] == "cancelled":
                cancelled.set()

        monkeypatch.setattr(Store, "save_agent_job", track)
    finally:
        release.set()
    assert cancelled.wait(timeout=2)
    assert calls == [entities[0]]
