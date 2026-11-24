import threading
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from epiq.ledger_api import LedgerDatabase
from epiq.ledger_web import (
    _web_reference,
    compact_command_observation,
    create_ledger_app,
    execute_bounded_command_batch,
)


def test_compact_command_observation_keeps_source_receipts_not_page_bodies() -> None:
    observation = compact_command_observation(
        ["epiq", "--project", "/tmp/project", "source-search", "--query", "Ada"],
        {
            "status": "ok",
            "command": "source-search",
            "data": {
                "query": "Ada   Lovelace\nGoogle Scholar",
                "sources": [
                    {
                        "title": "Ada Lovelace - Google Scholar",
                        "url": "https://example.test/ada",
                        "excerpt": "support " * 1_000,
                        "content": "full captured page " * 10000,
                    }
                ],
            },
            "errors": [],
            "warnings": [],
            "next_steps": [],
        },
    )

    receipt = observation["result"]["data"]
    assert receipt["query"] == "Ada Lovelace Google Scholar"
    assert receipt["sources_count"] == 1
    source = receipt["sources_preview"][0]
    assert source["title"] == "Ada Lovelace - Google Scholar"
    assert source["url"] == "https://example.test/ada"
    assert len(source["excerpt"]) == 4_000
    assert len(source["content"]) == 6_000
    assert len(str(observation)) < 11_000

    second = compact_command_observation(
        ["epiq", "--project", "/tmp/project", "source-search", "--query", "Ada"],
        {
            "status": "ok",
            "command": "source-search",
            "data": {
                "sources": [
                    {"title": "One", "url": "https://example.test/1", "content": "a" * 10_000},
                    {"title": "Two", "url": "https://example.test/2", "content": "b" * 10_000},
                ]
            },
        },
    )
    assert all(
        "content" not in item
        for item in second["result"]["data"]["sources_preview"]
    )


def test_bounded_command_batch_parallelizes_only_consecutive_source_searches() -> None:
    barrier = threading.Barrier(3, timeout=1)
    completed: list[str] = []
    lock = threading.Lock()

    def execute(command: list[str]) -> dict[str, object]:
        name = command[-1]
        if command[3] == "source-search":
            barrier.wait()
        with lock:
            completed.append(name)
        return {"type": "command_result", "name": name}

    prefix = ["epiq", "--project", "/tmp/project"]
    result = execute_bounded_command_batch(
        [
            [*prefix, "question", "before"],
            [*prefix, "source-search", "one"],
            [*prefix, "source-search", "two"],
            [*prefix, "source-search", "three"],
            [*prefix, "record", "after"],
        ],
        execute,
        source_search_workers=3,
    )

    assert result["type"] == "command_batch_result"
    assert completed[0] == "before"
    assert completed[-1] == "after"
    assert {item["name"] for item in result["results"][1:4]} == {"one", "two", "three"}


def workspace_plan(_goal, _context, progress=None):
    if progress:
        progress("Prepared a photo enrichment plan")
    return {
        "summary": "Add profile photos for every faculty member.",
        "entity_kinds": ["Faculty"],
        "entities": [{"kind": "Faculty", "name": "Ada Lovelace"}],
        "questions": [
            {
                "kind": "Faculty",
                "name": "profile_image",
                "label": "Profile image",
                "value_type": "Image",
                "cardinality": "one",
                "volatility": "dynamic",
                "freshness_days": 365,
                "research_guidance": "Use an official portrait when available.",
            }
        ],
        "research": [
            {
                "kind": "Faculty",
                "question": "profile_image",
                "entity_names": ["Ada Lovelace"],
                "instructions": "Find an official portrait.",
            }
        ],
    }


def photo_research(_kind, _question, entities, progress=None):
    if progress:
        progress("Found an official portrait")
    return [
        {
            "entity_id": entities[0]["entity_id"],
            "status": "answered",
            "value": "https://images.example/ada.jpg",
            "source_title": "Official faculty profile",
            "source_url": "https://example.test/ada",
            "source_type": "web",
            "excerpt": "Official portrait of Ada Lovelace.",
            "confidence": "high",
            "source_published_at": None,
            "observed_as_of": "2026-11-21",
        }
    ]


def action_plan(kind: str, target_rows: int) -> dict:
    return {
        "objective": f"Build a useful {kind} dataset.",
        "intended_use": "Compare collected rows.",
        "primary_kind": kind,
        "target_rows": target_rows,
        "coverage_categories": ["general"],
        "priority_fields": [],
        "completion_criteria": ["Target row count reached."],
    }


def action_completion() -> dict:
    return {
        "completed_categories": ["general"],
        "unresolved_leads": 0,
        "stop_reason": "completed",
        "blockers": [],
    }


def test_ledger_web_runs_core_spreadsheet_workflow_and_rebuilds(tmp_path: Path) -> None:
    root = tmp_path / "project"
    client = TestClient(create_ledger_app(root))
    assert client.get("/api/health").json()["storage"] == "git-ledger"
    assert client.post("/api/project", json={"name": "Market"}).status_code == 201
    client.post("/api/entity-kinds", json={"kind": "Company"})
    entity = client.post("/api/entities", json={"kind": "Company", "name": "Acme"}).json()[
        "entity_id"
    ]
    question = client.post(
        "/api/questions",
        json={"name": "employees", "subject_kind": "Company", "value_type": "Int"},
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "title": "Acme profile",
            "url": "https://example.test/acme",
            "retrieved_at": "2026-11-21",
            "excerpt": "Acme has 12 employees.",
        },
    ).json()["evidence_id"]
    claim = client.post(
        "/api/claims",
        json={
            "subject": entity,
            "question": question,
            "value": 12,
            "valid_from": "2026-11-21",
            "evidence_ids": [evidence],
        },
    ).json()["claim_id"]

    overview = client.get("/api/project").json()
    assert overview["entity_kinds"] == [{"kind": "Company", "entities": 1, "questions": 1}]
    cell = client.get("/api/matrix/Company").json()["rows"][0]["cells"]["employees"]
    assert cell["state"] == "Answered"
    assert cell["value"] == 12
    assert client.get("/api/claims-long").json()["claims"][0]["claim_id"] == claim

    (root / ".epiq" / "projection.sqlite").unlink()
    restarted = TestClient(create_ledger_app(root))
    assert (
        restarted.get("/api/matrix/Company").json()["rows"][0]["cells"]["employees"]["value"] == 12
    )
    assert restarted.get("/api/doctor").json()["healthy"] is True


def test_uninitialized_ledger_web_uses_frontend_compatible_error(tmp_path: Path) -> None:
    client = TestClient(create_ledger_app(tmp_path / "missing"))
    response = client.get("/api/project")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "project_not_found"


def test_ledger_web_exposes_read_only_lab_notebooks(tmp_path: Path) -> None:
    root = tmp_path / "project"
    database = LedgerDatabase(root, create=True, name="Notebook API")
    session = database.start_notebook(
        "Find credible competitors", scope={"kind": "Company"}
    )["session_id"]
    database.append_notebook(
        session, "lead", {"name": "Acme", "next": "verify the operator"}
    )
    client = TestClient(create_ledger_app(root))

    listed = client.get("/api/notebooks")
    assert listed.status_code == 200
    assert listed.json()[0]["session_id"] == session
    assert listed.json()[0]["entries"][0]["entry_type"] == "lead"
    assert client.get(f"/api/notebooks/{session}").json()["objective"] == (
        "Find credible competitors"
    )
    missing = client.get("/api/notebooks/nbk_missing")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "notebook_not_found"


def test_frontend_shell_is_not_cached_across_local_rebuilds(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>current</html>")
    client = TestClient(create_ledger_app(tmp_path / "missing", frontend=frontend))

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_ledger_web_can_close_and_reopen_configured_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    client = TestClient(create_ledger_app(root))
    project = client.post("/api/project", json={"name": "Closeable"}).json()["project"]

    assert client.get("/api/projects").json()[0]["active"] is True
    assert client.post("/api/projects/close", json={}).json() == {"closed": True}
    assert client.get("/api/health").json()["project"] == "closed"
    closed = client.get("/api/project")
    assert closed.status_code == 400
    assert closed.json()["error"]["code"] == "project_not_selected"
    assert client.get("/api/projects").json()[0]["active"] is False

    opened = client.post("/api/projects/open", json={"project_id": project["project_id"]})
    assert opened.status_code == 200
    assert opened.json()["active"] is True
    assert client.get("/api/project").json()["project"]["name"] == "Closeable"


def test_ledger_web_creates_and_switches_between_git_projects(
    tmp_path: Path, monkeypatch
) -> None:
    projects = tmp_path / "projects"
    first_root = projects / "first"
    monkeypatch.setenv("EPIQ_PROJECTS_DIR", str(projects))
    client = TestClient(create_ledger_app(first_root))
    first = client.post("/api/project", json={"name": "First"}).json()["project"]
    client.post("/api/projects/close", json={})
    client.app.state.research_jobs["old-project-job"] = {
        "job_id": "old-project-job",
        "job_type": "workspace_agent",
    }

    response = client.post("/api/projects", json={"name": "Restaurants in Bourne, MA"})

    assert response.status_code == 201
    created = response.json()
    assert created["name"] == "Restaurants in Bourne, MA"
    assert Path(created["path"]).name == "restaurants-in-bourne-ma"
    assert (Path(created["path"]) / "epiq.toml").exists()
    assert client.get("/api/project").json()["project"]["name"] == created["name"]
    assert client.get("/api/research/jobs").json() == []
    listed = client.get("/api/projects").json()
    assert {item["name"] for item in listed} == {"First", created["name"]}
    assert next(item for item in listed if item["name"] == created["name"])["active"] is True

    restarted = TestClient(create_ledger_app(first_root))
    assert restarted.get("/api/project").json()["project"]["name"] == created["name"]

    reopened = client.post("/api/projects/open", json={"project_id": first["project_id"]})
    assert reopened.status_code == 200
    assert reopened.json()["name"] == "First"
    assert client.get("/api/project").json()["project"]["name"] == "First"


def test_ledger_web_refuses_to_switch_projects_during_active_work(
    tmp_path: Path, monkeypatch
) -> None:
    projects = tmp_path / "projects"
    first_root = projects / "first"
    second_root = projects / "second"
    monkeypatch.setenv("EPIQ_PROJECTS_DIR", str(projects))
    first = LedgerDatabase(first_root, create=True, name="First").project()
    second = LedgerDatabase(second_root, create=True, name="Second").project()
    client = TestClient(create_ledger_app(first_root))
    client.app.state.research_jobs["active"] = {
        "job_id": "active",
        "status": "running",
    }

    response = client.post("/api/projects/open", json={"project_id": second["project_id"]})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "research_active"
    assert client.get("/api/project").json()["project"]["project_id"] == first["project_id"]


def test_ledger_web_review_visibility_and_retraction_are_durable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    client = TestClient(create_ledger_app(root))
    client.post("/api/project", json={"name": "Review"})
    client.post("/api/entity-kinds", json={"kind": "Item"})
    entity = client.post("/api/entities", json={"kind": "Item", "name": "One"}).json()["entity_id"]
    question = client.post(
        "/api/questions",
        json={"name": "value", "subject_kind": "Item", "value_type": "Int"},
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "title": "Source",
            "url": "https://example.test",
            "retrieved_at": "2026-11-21",
            "excerpt": "Value is one.",
        },
    ).json()["evidence_id"]
    claim = client.post(
        "/api/claims",
        json={
            "subject": entity,
            "question": question,
            "value": 1,
            "valid_from": "2026-11-21",
            "evidence_ids": [evidence],
        },
    ).json()["claim_id"]

    assert (
        client.post(
            f"/api/evidence/{evidence}/assess",
            json={"status": "accepted", "reason": "Checked"},
        ).status_code
        == 201
    )
    assert client.post(f"/api/claims/{claim}/review", json={"reason": "Checked"}).status_code == 200
    reviewed = client.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"]
    assert reviewed["lineage"][0]["review"] == {
        "status": "accepted",
        "reason": "Checked",
    }
    client.post(f"/api/questions/{question}/retire", json={"reason": "Redesign"})
    assert client.get("/api/matrix/Item").json()["questions"] == []
    client.post(f"/api/questions/{question}/restore", json={"reason": "Keep"})
    client.post(f"/api/entities/{entity}/retire", json={"reason": "Hidden"})
    assert client.get("/api/matrix/Item").json()["rows"] == []
    client.post(f"/api/entities/{entity}/restore", json={"reason": "Keep"})
    client.post(f"/api/claims/{claim}/retract", json={"reason": "Incorrect"})
    assert client.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"]["state"] == "Unasked"

    (root / ".epiq" / "projection.sqlite").unlink()
    restarted = TestClient(create_ledger_app(root))
    assert (
        restarted.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"]["state"] == "Unasked"
    )


def test_question_challenges_round_trip_through_ledger_web(tmp_path: Path) -> None:
    root = tmp_path / "project"
    client = TestClient(create_ledger_app(root))
    client.post("/api/project", json={"name": "Questions"})
    client.post("/api/entity-kinds", json={"kind": "Item"})
    question = client.post(
        "/api/questions",
        json={"name": "value", "subject_kind": "Item", "value_type": "String"},
    ).json()["question_id"]
    challenge = client.post(
        f"/api/questions/{question}/challenges",
        json={"problem": "definition_ambiguity", "explanation": "Unit is unclear"},
    ).json()["challenge_id"]

    open_items = client.get("/api/question-challenges", params={"status": "open"}).json()
    assert open_items[0]["challenge_id"] == challenge
    client.post(
        f"/api/question-challenges/{challenge}/resolve",
        json={"status": "resolved", "resolution": "Unit is count"},
    )
    assert client.get("/api/question-challenges", params={"status": "open"}).json() == []
    assert client.get("/api/question-challenges").json()[0]["resolution"] == "Unit is count"

    (root / ".epiq" / "projection.sqlite").unlink()
    restarted = TestClient(create_ledger_app(root))
    assert restarted.get("/api/question-challenges").json()[0]["status"] == "resolved"


def test_not_found_remains_distinct_after_projection_rebuild(tmp_path: Path) -> None:
    root = tmp_path / "project"
    client = TestClient(create_ledger_app(root))
    client.post("/api/project", json={"name": "Research"})
    client.post("/api/entity-kinds", json={"kind": "Item"})
    entity = client.post("/api/entities", json={"kind": "Item", "name": "One"}).json()["entity_id"]
    question = client.post(
        "/api/questions",
        json={"name": "value", "subject_kind": "Item", "value_type": "String"},
    ).json()["question_id"]
    result = client.post(
        "/api/research/not-found",
        json={
            "subject": entity,
            "question": question,
            "query": "item value",
            "notes": "No reliable source found",
        },
    )
    assert result.status_code == 201
    cell = client.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"]
    assert cell["state"] == "NotFound"
    assert cell["research"]["notes"] == "No reliable source found"

    (root / ".epiq" / "projection.sqlite").unlink()
    restarted = TestClient(create_ledger_app(root))
    assert (
        restarted.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"]["state"] == "NotFound"
    )


def test_workspace_agent_console_plans_then_applies_to_ledger(tmp_path: Path) -> None:
    root = tmp_path / "project"
    client = TestClient(
        create_ledger_app(
            root,
            workspace_agent_runner=workspace_plan,
            research_runner=photo_research,
        )
    )
    client.post("/api/project", json={"name": "Faculty research"})

    launched = client.post("/api/workspace-agent/jobs", json={"message": "Add photos"})
    assert launched.status_code == 202
    job_id = launched.json()["job_id"]
    for _ in range(100):
        job = next(
            item for item in client.get("/api/research/jobs").json() if item["job_id"] == job_id
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["outcome"] == "workspace_proposal"
    assert job["approval_status"] == "pending"
    assert [item["argv"][3] for item in job["command_transcript"]] == [
        "kind",
        "entity",
        "question",
        "research",
    ]
    assert all(item["status"] == "proposed" for item in job["command_transcript"])
    assert client.get("/api/project").json()["entity_kinds"] == []

    approved = client.post(f"/api/workspace-agent/jobs/{job_id}/approve")
    assert approved.status_code == 202
    assert approved.json()["approval_status"] == "approved"
    for _ in range(100):
        job = next(
            item for item in client.get("/api/research/jobs").json() if item["job_id"] == job_id
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    assert not job.get("error"), job["error"]
    assert job["status"] == "completed"
    assert [item["result"]["status"] for item in job["command_results"]] == [
        "ok",
        "ok",
        "ok",
        "ok",
    ]
    assert job["command_results"][-1]["result"]["data"] == {
        "written": 1,
        "not_found": 0,
    }
    matrix = client.get("/api/matrix/Faculty").json()
    assert [row["name"] for row in matrix["rows"]] == ["Ada Lovelace"]
    assert matrix["questions"][0]["value_type"] == "Image"
    assert matrix["rows"][0]["cells"]["profile_image"]["value"] == (
        "https://images.example/ada.jpg"
    )

    proposals = LedgerDatabase(root).proposals()
    assert proposals[0]["proposal_type"] == "workspace_agent"

    restarted = TestClient(create_ledger_app(root, research_runner=photo_research))
    history = restarted.get("/api/research/jobs").json()
    assert len(history) == 1
    assert history[0]["historical"] is True
    assert history[0]["user_message"] == "Add photos"
    assert history[0]["assistant_summary"] == "Add profile photos for every faculty member."
    assert history[0]["approval_status"] == "approved"
    assert history[0]["command_transcript"][-1]["argv"][3] == "research"


def test_workspace_agent_action_loop_streams_and_executes_cli(tmp_path: Path) -> None:
    root = tmp_path / "project"

    class ActionRunner:
        def __init__(self) -> None:
            self.actions = iter(
                [
                    {
                        "action": "message",
                        "message": "I found the first company to add.",
                        "argv": [],
                        "decision": None,
                        "plan": action_plan("Company", 1),
                        "completion": None,
                    },
                    {
                        "action": "command",
                        "message": "Create the Company row type.",
                        "argv": [
                            "epiq",
                            "--project",
                            "PROJECT_PATH",
                            "kind",
                            "Company",
                        ],
                        "decision": None,
                        "plan": None,
                        "completion": None,
                    },
                    {
                        "action": "command",
                        "message": "Add Acme as a Company row.",
                        "argv": [
                            "epiq",
                            "--project",
                            "PROJECT_PATH",
                            "entity",
                            "Company",
                            "Acme",
                        ],
                        "decision": None,
                        "plan": None,
                        "completion": None,
                    },
                    {
                        "action": "finish",
                        "message": "Added Acme and verified the command result.",
                        "argv": [],
                        "decision": None,
                        "plan": None,
                        "completion": action_completion(),
                    },
                ]
            )
            self.observations = []

        def next_action(self, _goal, _context, observation, _progress=None):
            self.observations.append(observation)
            return next(self.actions)

    runner = ActionRunner()
    client = TestClient(create_ledger_app(root, workspace_agent_runner=runner))
    client.post("/api/project", json={"name": "Action loop"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Add useful companies", "auto_approve": True},
    ).json()

    for _ in range(200):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["status"] == "completed", job.get("error")
    assert [event["type"] for event in job["events"]] == [
        "user_message",
        "research_plan",
        "agent_message",
        "command_proposed",
        "command_started",
        "ledger_transaction",
        "command_result",
        "command_proposed",
        "command_started",
        "ledger_transaction",
        "command_result",
        "run_completed",
    ]
    assert runner.observations[-1]["type"] == "command_result"
    assert runner.observations[-1]["result"]["data"]["entity_id"].startswith("ent_")
    assert "projection" not in runner.observations[-1]["result"]["data"]
    assert client.get("/api/matrix/Company").json()["rows"][0]["name"] == "Acme"
    ledger_events = [event for event in job["events"] if event["type"] == "ledger_transaction"]
    assert all(event["data"]["transaction_id"].startswith("txn_") for event in ledger_events)
    assert ledger_events[-1]["data"]["operations"][0]["type"] == "entity.create"
    assert ledger_events[-1]["data"]["affected_kinds"] == ["Company"]
    assert ledger_events[-1]["data"]["affected_rows"][0]["entity_name"] == "Acme"

    restarted = TestClient(create_ledger_app(root, workspace_agent_runner=runner))
    restored = next(
        item
        for item in restarted.get("/api/research/jobs").json()
        if item["job_id"] == job["job_id"]
    )
    assert restored["historical"] is True
    assert restored["user_message"] == "Add useful companies"
    assert any(event["type"] == "ledger_transaction" for event in restored["events"])


def test_workspace_agent_action_loop_waits_for_command_approval(tmp_path: Path) -> None:
    root = tmp_path / "project"

    class ActionRunner:
        def __init__(self) -> None:
            self.actions = iter(
                [
                    {
                        "action": "command",
                        "message": "Create the Company row type.",
                        "argv": [
                            "epiq",
                            "--project",
                            "PROJECT_PATH",
                            "kind",
                            "Company",
                        ],
                        "decision": None,
                        "plan": action_plan("Company", 0),
                        "completion": None,
                    },
                    {
                        "action": "finish",
                        "message": "The approved command completed.",
                        "argv": [],
                        "decision": None,
                        "plan": None,
                        "completion": action_completion(),
                    },
                ]
            )

        def next_action(self, _goal, _context, _observation, _progress=None):
            return next(self.actions)

    client = TestClient(create_ledger_app(root, workspace_agent_runner=ActionRunner()))
    client.post("/api/project", json={"name": "Approval loop"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Add useful companies", "auto_approve": False},
    ).json()

    for _ in range(200):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job.get("approval_status") == "pending_action":
            break
        time.sleep(0.01)
    assert job["status"] == "waiting_for_user"
    assert client.get("/api/project").json()["entity_kinds"] == []

    approved = client.post(f"/api/workspace-agent/jobs/{job['job_id']}/approve")
    assert approved.status_code == 202
    for _ in range(200):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    assert job["status"] == "completed", job.get("error")
    assert client.get("/api/project").json()["entity_kinds"] == [
        {"kind": "Company", "entities": 0, "questions": 0}
    ]


def test_workspace_agent_executes_command_batch_in_one_turn(tmp_path: Path) -> None:
    root = tmp_path / "project"

    class BatchRunner:
        def __init__(self) -> None:
            self.calls = 0
            self.observations = []

        def next_action(self, _goal, _context, observation, _progress=None):
            self.calls += 1
            self.observations.append(observation)
            if self.calls == 1:
                return {
                    "action": "command",
                    "message": "Create the table and its first rows as one batch.",
                    "argv": [],
                    "commands": [
                        ["epiq", "--project", "PROJECT_PATH", "kind", "Company"],
                        [
                            "epiq",
                            "--project",
                            "PROJECT_PATH",
                            "entity-batch",
                            "Company",
                            "--names-json",
                            '["Acme", "Beta"]',
                        ],
                    ],
                    "decision": None,
                    "plan": action_plan("Company", 2),
                    "completion": None,
                }
            return {
                "action": "finish",
                "message": "Created both rows.",
                "argv": [],
                "commands": [],
                "decision": None,
                "plan": None,
                "completion": action_completion(),
            }

    runner = BatchRunner()
    client = TestClient(create_ledger_app(root, workspace_agent_runner=runner))
    client.post("/api/project", json={"name": "Batch action loop"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Add two companies", "auto_approve": True},
    ).json()
    for _ in range(200):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["status"] == "completed", job.get("error")
    assert runner.calls == 2
    assert runner.observations[1]["type"] == "command_batch_result"
    assert runner.observations[1]["completed_count"] == 2
    assert [row["name"] for row in client.get("/api/matrix/Company").json()["rows"]] == [
        "Acme",
        "Beta",
    ]
    proposed = [event for event in job["events"] if event["type"] == "command_proposed"]
    assert [event["data"]["batch_index"] for event in proposed] == [0, 1]


def test_workspace_agent_action_loop_recovers_from_rejected_command(tmp_path: Path) -> None:
    root = tmp_path / "project"

    class ActionRunner:
        def __init__(self) -> None:
            self.observations = []
            self.actions = iter(
                [
                    {
                        "action": "command",
                        "message": "Try a command outside the agent contract.",
                        "argv": ["epiq", "--project", "PROJECT_PATH", "init"],
                        "decision": None,
                        "plan": action_plan("Company", 0),
                        "completion": None,
                    },
                    {
                        "action": "command",
                        "message": "Inspect the supported project commands.",
                        "argv": ["epiq", "--project", "PROJECT_PATH", "--help"],
                        "decision": None,
                        "plan": None,
                        "completion": None,
                    },
                    {
                        "action": "finish",
                        "message": "Recovered from the rejected command.",
                        "argv": [],
                        "decision": None,
                        "plan": None,
                        "completion": action_completion(),
                    },
                ]
            )

        def next_action(self, _goal, _context, observation, _progress=None):
            self.observations.append(observation)
            return next(self.actions)

    runner = ActionRunner()
    client = TestClient(create_ledger_app(root, workspace_agent_runner=runner))
    client.post("/api/project", json={"name": "Recovery loop"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Inspect the project", "auto_approve": True},
    ).json()

    for _ in range(200):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["status"] == "completed", job.get("error")
    assert runner.observations[1]["type"] == "command_rejected"
    assert runner.observations[2]["type"] == "command_result"
    assert "command_rejected" in [event["type"] for event in job["events"]]


def test_control_center_rejects_opaque_research_commands(tmp_path: Path) -> None:
    root = tmp_path / "project"
    observations = []
    actions = iter(
        [
            {
                "action": "command",
                "message": "Try opaque research.",
                "argv": [
                    "epiq",
                    "--project",
                    "PROJECT_PATH",
                    "research",
                    "--kind",
                    "Company",
                    "--question",
                    "website",
                ],
                "decision": None,
                "plan": action_plan("Company", 0),
                "completion": None,
            },
            {
                "action": "finish",
                "message": "The opaque path is unavailable.",
                "argv": [],
                "decision": None,
                "plan": None,
                "completion": {
                    **action_completion(),
                    "stop_reason": "tool_blocked",
                    "blockers": ["Opaque research is deliberately unavailable."],
                },
            },
        ]
    )

    class Runner:
        def next_action(self, _goal, _context, observation, _progress=None):
            observations.append(observation)
            return next(actions)

    client = TestClient(create_ledger_app(root, workspace_agent_runner=Runner()))
    client.post("/api/project", json={"name": "Opaque research guard"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Research companies", "auto_approve": True},
    ).json()
    for _ in range(200):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["status"] == "completed", job.get("error")
    assert observations[1]["type"] == "command_rejected"
    assert "source-search" in observations[1]["error"]
    assert not any(event["type"] == "command_started" for event in job["events"])


def test_workspace_agent_finish_gate_enforces_declared_target(tmp_path: Path) -> None:
    root = tmp_path / "project"
    observations = []
    actions = iter(
        [
            {
                "action": "command",
                "message": "Add the first company.",
                "argv": ["epiq", "--project", "PROJECT_PATH", "entity", "Company", "Acme"],
                "decision": None,
                "plan": action_plan("Company", 2),
                "completion": None,
            },
            {
                "action": "finish",
                "message": "One company is enough.",
                "argv": [],
                "decision": None,
                "plan": None,
                "completion": action_completion(),
            },
            {
                "action": "command",
                "message": "The gate is right; add the second company.",
                "argv": ["epiq", "--project", "PROJECT_PATH", "entity", "Company", "Beta"],
                "decision": None,
                "plan": None,
                "completion": None,
            },
            {
                "action": "finish",
                "message": "The declared target is now satisfied.",
                "argv": [],
                "decision": None,
                "plan": None,
                "completion": action_completion(),
            },
        ]
    )

    class ActionRunner:
        def next_action(self, _goal, _context, observation, _progress=None):
            observations.append(observation)
            return next(actions)

    LedgerDatabase(root, create=True, name="Finish gate").define_kind("Company")
    client = TestClient(create_ledger_app(root, workspace_agent_runner=ActionRunner()))
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Collect two companies", "auto_approve": True},
    ).json()
    for _ in range(200):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["status"] == "completed", job.get("error")
    rejected = next(item for item in observations if item and item["type"] == "finish_rejected")
    assert "has 1 rows; the plan targets 2" in rejected["reasons"][0]
    assert [item["name"] for item in LedgerDatabase(root).entities(kind="Company")] == [
        "Acme",
        "Beta",
    ]


def test_workspace_agent_can_retract_an_exact_claim(tmp_path: Path) -> None:
    root = tmp_path / "project"
    claim_id = ""

    def correction_plan(_goal, _context, progress=None):
        return {
            "summary": "Retract the address attached to the wrong restaurant.",
            "entity_kinds": [],
            "entities": [],
            "questions": [],
            "retractions": [
                {"claim_id": claim_id, "reason": "wrong restaurant location"}
            ],
            "research": [],
        }

    client = TestClient(
        create_ledger_app(
            root,
            workspace_agent_runner=correction_plan,
            research_runner=photo_research,
        )
    )
    client.post("/api/project", json={"name": "Restaurants"})
    client.post("/api/entity-kinds", json={"kind": "Restaurant"})
    entity = client.post(
        "/api/entities", json={"kind": "Restaurant", "name": "The Chart Room"}
    ).json()["entity_id"]
    question = client.post(
        "/api/questions",
        json={"name": "address", "subject_kind": "Restaurant", "value_type": "String"},
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "title": "Operator page",
            "url": "https://example.test/chart-room",
            "retrieved_at": "2026-11-21",
            "excerpt": "565 Eden St, Bar Harbor, ME",
        },
    ).json()["evidence_id"]
    claim_id = client.post(
        "/api/claims",
        json={
            "subject": entity,
            "question": question,
            "value": "565 Eden St, Bar Harbor, ME",
            "valid_from": "2026-11-21",
            "evidence_ids": [evidence],
        },
    ).json()["claim_id"]

    job_id = client.post(
        "/api/workspace-agent/jobs", json={"message": "Retract the wrong address"}
    ).json()["job_id"]
    for _ in range(100):
        job = next(
            item for item in client.get("/api/research/jobs").json()
            if item["job_id"] == job_id
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["command_transcript"][0]["argv"][3:] == [
        "retract-claim",
        claim_id,
        "--reason",
        "wrong restaurant location",
    ]
    approved = client.post(f"/api/workspace-agent/jobs/{job_id}/approve")
    assert approved.status_code == 202
    assert client.get("/api/matrix/Restaurant").json()["rows"][0]["cells"]["address"][
        "state"
    ] == "Unasked"


def test_workspace_agent_records_user_decision_and_continues_same_pi_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    calls = []

    def decision_plan(goal, _context, progress=None):
        calls.append(goal)
        if len(calls) == 1:
            progress("Found a useful edge-case candidate")
            return {
                "summary": "One candidate is useful but falls just outside the stated scope.",
                "entity_kinds": [],
                "entities": [],
                "questions": [],
                "retractions": [],
                "replacements": [],
                "research": [],
                "decisions": [
                    {
                        "decision_id": "include_acme",
                        "question": "Add Acme despite the scope mismatch?",
                        "context": "Acme has strong evidence but is adjacent to the niche.",
                        "options": [
                            {
                                "value": "yes",
                                "label": "Yes, include it",
                                "consequence": "Acme becomes part of the comparison set.",
                            },
                            {
                                "value": "no",
                                "label": "No, keep scope strict",
                                "consequence": "Acme remains only a notebook lead.",
                            },
                        ],
                        "recommended_option": "no",
                    }
                ],
            }
        return {
            "summary": "Apply the user's explicit inclusion decision.",
            "entity_kinds": ["Company"],
            "entities": [{"kind": "Company", "name": "Acme"}],
            "questions": [],
            "retractions": [],
            "replacements": [],
            "research": [],
            "decisions": [],
        }

    client = TestClient(create_ledger_app(root, workspace_agent_runner=decision_plan))
    client.post("/api/project", json={"name": "Decisions"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Find niche companies", "auto_approve": True},
    ).json()
    for _ in range(200):
        first = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if first["status"] == "waiting_for_user":
            break
        time.sleep(0.01)

    assert first["approval_status"] == "awaiting_input"
    assert [event["type"] for event in first["events"]] == [
        "user_message",
        "agent_started",
        "platform_update",
        "platform_update",
        "agent_message",
        "decision_requested",
    ]
    continued = client.post(
        f"/api/workspace-agent/jobs/{first['job_id']}/decisions/include_acme",
        json={"option": "yes", "reason": "Include adjacent firms for recall."},
    )
    assert continued.status_code == 202
    next_job_id = continued.json()["job_id"]
    for _ in range(200):
        second = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == next_job_id
        )
        if second["status"] == "completed" and second.get("approval_status") == "approved":
            break
        time.sleep(0.01)

    assert "The user selected: Yes, include it (yes)." in calls[1]
    assert "Include adjacent firms for recall." in calls[1]
    assert client.get("/api/matrix/Company").json()["rows"][0]["name"] == "Acme"
    refreshed_first = next(
        item
        for item in client.get("/api/research/jobs").json()
        if item["job_id"] == first["job_id"]
    )
    assert refreshed_first["decision_response"]["reason"] == (
        "Include adjacent firms for recall."
    )
    assert refreshed_first["events"][-1]["type"] == "decision_answered"


def test_workspace_agent_can_replace_an_answered_claim_preserving_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    claim_id = ""

    def correction_plan(_goal, _context, progress=None):
        return {
            "summary": "Normalize the address value while preserving its evidence.",
            "entity_kinds": [],
            "entities": [],
            "questions": [],
            "retractions": [],
            "replacements": [
                {
                    "claim_id": claim_id,
                    "value": "290 Shore Rd., Bourne, MA 02532",
                    "reason": "Normalize address capitalization",
                }
            ],
            "research": [],
        }

    client = TestClient(
        create_ledger_app(
            root,
            workspace_agent_runner=correction_plan,
            research_runner=photo_research,
        )
    )
    client.post("/api/project", json={"name": "Restaurants"})
    client.post("/api/entity-kinds", json={"kind": "Restaurant"})
    entity = client.post(
        "/api/entities", json={"kind": "Restaurant", "name": "The Lobster Trap"}
    ).json()["entity_id"]
    question = client.post(
        "/api/questions",
        json={"name": "address", "subject_kind": "Restaurant", "value_type": "String"},
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "title": "Contact page",
            "url": "https://example.test/contact",
            "retrieved_at": "2026-11-21",
            "excerpt": "290 SHORE RD., BOURNE, MA 02532",
        },
    ).json()["evidence_id"]
    claim_id = client.post(
        "/api/claims",
        json={
            "subject": entity,
            "question": question,
            "value": "290 SHORE RD., BOURNE, MA 02532",
            "valid_from": "2026-11-21",
            "evidence_ids": [evidence],
        },
    ).json()["claim_id"]

    job_id = client.post(
        "/api/workspace-agent/jobs", json={"message": "Normalize the address"}
    ).json()["job_id"]
    for _ in range(100):
        job = next(
            item for item in client.get("/api/research/jobs").json()
            if item["job_id"] == job_id
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job["command_transcript"][0]["argv"][3] == "supersede-claim"
    assert client.post(f"/api/workspace-agent/jobs/{job_id}/approve").status_code == 202
    cell = client.get("/api/matrix/Restaurant").json()["rows"][0]["cells"]["address"]
    assert cell["value"] == "290 Shore Rd., Bourne, MA 02532"
    assert cell["lineage"][0]["evidence_id"] == evidence
    claims = LedgerDatabase(root).claims(include_closed=True)
    original = next(item for item in claims if item["claim_id"] == claim_id)
    assert original["value"] == "290 SHORE RD., BOURNE, MA 02532"
    assert cell["claims"][0]["claim_id"] != claim_id


def test_workspace_agent_receives_cell_coverage(tmp_path: Path) -> None:
    root = tmp_path / "project"
    observed = {}

    def capture_context(_goal, context, progress=None):
        observed.update(context)
        return {
            "summary": "No changes needed.",
            "entity_kinds": [],
            "entities": [],
            "questions": [],
            "research": [],
        }

    database = LedgerDatabase(root, create=True, name="Coverage")
    database.define_kind("Faculty")
    answered = database.create_entity("Faculty", "Answered")["entity_id"]
    missing = database.create_entity("Faculty", "Missing")["entity_id"]
    question = database.define_question("birth_year", "Faculty", "Year")["question_id"]
    database.record(
        answered,
        question,
        1815,
        valid_from="1815-01-01",
        source_title="Biography",
        source_type="web",
        url="https://example.test/answered",
        retrieved_at="2026-11-21",
        excerpt="Born in 1815.",
    )
    database.record_not_found(
        missing,
        question,
        "Missing birth year",
        "No reliable source found",
    )
    notebook = database.start_notebook(
        "Find birth years", scope={"kind": "Faculty"}
    )["session_id"]
    database.append_notebook(
        notebook, "lead", {"name": "Missing", "next": "search an archive"}
    )
    client = TestClient(create_ledger_app(root, workspace_agent_runner=capture_context))
    job_id = client.post(
        "/api/workspace-agent/jobs", json={"message": "What remains?"}
    ).json()["job_id"]
    for _ in range(100):
        job = next(
            item for item in client.get("/api/research/jobs").json() if item["job_id"] == job_id
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert observed["tables"][0]["questions"][0]["coverage"] == {
        "answered": 1,
        "not_found": 1,
        "unasked": 0,
    }
    assert observed["lab_notebooks"][0]["entries"][0]["content"]["next"] == (
        "search an archive"
    )


def test_failed_cli_research_is_passed_to_pi_for_bounded_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "project"
    database = LedgerDatabase(root, create=True, name="Recovery")
    database.define_kind("Company")
    database.create_entity("Company", "Acme")
    database.define_question("website", "Company", "URL")
    goals = []

    def planner(goal, _context, progress=None):
        goals.append(goal)
        if len(goals) == 1:
            return {
                "summary": "Research the website.",
                "entity_kinds": [],
                "entities": [],
                "questions": [],
                "research": [{
                    "kind": "Company", "question": "website",
                    "entity_names": ["Acme"], "instructions": "Use the official site.",
                }],
            }
        return {
            "summary": "Stop after diagnosing the invalid value shape.",
            "entity_kinds": [], "entities": [], "questions": [], "research": [],
        }

    def failed_command(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=2,
            stdout='{"status":"error","errors":[{"code":"invalid_reference_value",'
            '"message":"missing name"}]}',
            stderr="",
        )

    monkeypatch.setattr("epiq.ledger_web.subprocess.run", failed_command)
    client = TestClient(create_ledger_app(root, workspace_agent_runner=planner))
    parent_id = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Find Acme's website", "auto_approve": True},
    ).json()["job_id"]

    for _ in range(300):
        jobs = client.get("/api/research/jobs").json()
        parent = next(item for item in jobs if item["job_id"] == parent_id)
        children = [item for item in jobs if item.get("recovery_of_job_id") == parent_id]
        if parent["status"] == "failed" and children and children[0]["status"] == "completed":
            break
        time.sleep(0.01)

    assert parent["status"] == "failed"
    assert len(children) == 1
    assert children[0]["recovery_depth"] == 1
    assert "invalid_reference_value" in goals[1]
    assert "Do not repeat the same command unchanged" in goals[1]


def test_workspace_plan_preserves_research_rows_beyond_seed_limit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    names = [f"Citation {index}" for index in range(16)]

    def citation_plan(_goal, _context, progress=None):
        return {
            "summary": "Create and research citation rows.",
            "entity_kinds": ["Citation"],
            "entities": [{"kind": "Citation", "name": name} for name in names],
            "questions": [
                {
                    "kind": "Citation",
                    "name": "source_url",
                    "label": "Source URL",
                    "value_type": "URL",
                    "cardinality": "one",
                    "volatility": "dynamic",
                    "freshness_days": 90,
                    "research_guidance": "Find the canonical source URL.",
                }
            ],
            "research": [
                {
                    "kind": "Citation",
                    "question": "source_url",
                    "entity_names": names,
                    "instructions": "Research every citation.",
                }
            ],
        }

    client = TestClient(create_ledger_app(root, workspace_agent_runner=citation_plan))
    client.post("/api/project", json={"name": "Citations"})
    job_id = client.post(
        "/api/workspace-agent/jobs", json={"message": "Get citations"}
    ).json()["job_id"]
    for _ in range(100):
        job = next(
            item for item in client.get("/api/research/jobs").json() if item["job_id"] == job_id
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert [item["name"] for item in job["workspace_plan"]["entities"]] == names
    entity_commands = [
        item["argv"] for item in job["command_transcript"] if item["argv"][3:4] == ["entity"]
    ]
    assert entity_commands[-1][-1] == "Citation 15"


def test_control_center_executes_schema_through_cli_and_keeps_exact_envelope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"

    def schema_only(_goal, _context, progress=None):
        return {
            "summary": "Create a company table.",
            "entity_kinds": ["Company"],
            "entities": [{"kind": "Company", "name": "Acme"}],
            "questions": [],
            "research": [],
        }

    client = TestClient(create_ledger_app(root, workspace_agent_runner=schema_only))
    client.post("/api/project", json={"name": "CLI control center"})
    launched = client.post("/api/workspace-agent/jobs", json={"message": "Add Acme"}).json()
    for _ in range(100):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    approved = client.post(f"/api/workspace-agent/jobs/{job['job_id']}/approve").json()
    assert approved["status"] == "completed"
    assert [item["result"]["status"] for item in approved["command_results"]] == ["ok", "ok"]
    assert approved["command_results"][0]["result"]["argv"] == [
        "epiq",
        "ledger",
        "kind",
        "--path",
        str(root),
        "Company",
    ]
    assert client.get("/api/matrix/Company").json()["rows"][0]["name"] == "Acme"


def test_control_center_yolo_mode_applies_plan_without_separate_approval(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"

    def schema_only(_goal, _context, progress=None):
        return {
            "summary": "Create a company table immediately.",
            "entity_kinds": ["Company"],
            "entities": [{"kind": "Company", "name": "Acme"}],
            "questions": [],
            "research": [],
        }

    client = TestClient(create_ledger_app(root, workspace_agent_runner=schema_only))
    client.post("/api/project", json={"name": "YOLO control center"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Add Acme", "auto_approve": True},
    ).json()
    for _ in range(100):
        job = next(
            item
            for item in client.get("/api/research/jobs").json()
            if item["job_id"] == launched["job_id"]
        )
        if job["status"] in {"completed", "failed"} and job.get("approval_status") != "applying":
            break
        time.sleep(0.01)

    assert job["status"] == "completed"
    assert job["approval_status"] == "approved"
    assert job["auto_approve"] is True
    assert client.get("/api/matrix/Company").json()["rows"][0]["name"] == "Acme"
    assert any("YOLO mode enabled" in item["message"] for item in job["messages"])


def test_curator_repeatedly_enriches_until_two_cycles_have_no_changes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    goals = []

    def curator_plan(goal, _context, progress=None):
        goals.append(goal)
        return {
            "summary": "Add the most useful company row.",
            "entity_kinds": ["Company"],
            "entities": [{"kind": "Company", "name": "Acme"}],
            "questions": [],
            "research": [],
        }

    client = TestClient(create_ledger_app(root, workspace_agent_runner=curator_plan))
    client.post("/api/project", json={"name": "Curated companies"})
    launched = client.post(
        "/api/curator",
        json={
            "objective": "Build a useful company comparison dataset",
            "budget_usd": 10,
            "max_cycles": 5,
            "max_research_cells": 10,
        },
    )
    assert launched.status_code == 202

    for _ in range(300):
        state = client.get("/api/curator").json()
        if state["status"] == "awaiting_approval":
            break
        time.sleep(0.01)
    assert state["strategy"]["target_example_count"] == 25
    assert client.post("/api/curator/approve").status_code == 202

    for _ in range(300):
        state = client.get("/api/curator").json()
        if state["status"] in {"completed", "failed", "stopped"}:
            break
        time.sleep(0.01)

    assert state["status"] == "completed"
    assert state["completed_cycles"] == 5
    assert state["cycles"][0]["written"] == 2
    assert state["cycles"][1]["written"] == 0
    assert state["cycles"][2]["written"] == 0
    assert state["cycles"][0]["phase"] == "breadth"
    assert state["cycles"][0]["delta"]["total_rows"] == 1
    assert "four escalating discovery strategies" in state["message"]
    assert state["estimated_spend_usd"] == 4.5
    assert state["budget_usd"] == 10
    assert len(goals) == 5
    assert "Previous cycle outcome" in goals[1]
    assert "substantial bounded work package" in goals[0]
    assert "Mine unresolved notebook leads" in goals[3]
    assert client.get("/api/matrix/Company").json()["rows"][0]["name"] == "Acme"

    extended = client.post(
        "/api/curator/add-budget",
        json={"amount_usd": 0.75, "direction": "Focus next on funding"},
    )
    assert extended.status_code == 202
    assert extended.json()["budget_usd"] == 10.75
    for _ in range(300):
        state = client.get("/api/curator").json()
        if state["status"] in {"completed", "failed", "stopped"}:
            break
        time.sleep(0.01)
    assert state["completed_cycles"] == 9
    assert state["estimated_spend_usd"] == 7.5
    assert "Focus next on funding" in goals[-1]

    restarted = TestClient(create_ledger_app(root, workspace_agent_runner=curator_plan))
    persisted = restarted.get("/api/curator").json()
    assert persisted["status"] == "completed"
    assert persisted["objective"] == "Build a useful company comparison dataset"


def test_curator_recovers_from_one_failed_cycle_instead_of_pausing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    goals = []

    def curator_plan(goal, _context, progress=None):
        goals.append(goal)
        if len(goals) == 1:
            raise RuntimeError("temporary source provider failure")
        return {
            "summary": "Recover by adding the verified company row.",
            "entity_kinds": ["Company"],
            "entities": [{"kind": "Company", "name": "Acme"}],
            "questions": [],
            "research": [],
        }

    client = TestClient(create_ledger_app(root, workspace_agent_runner=curator_plan))
    client.post("/api/project", json={"name": "Resilient curator"})
    client.post(
        "/api/curator",
        json={
            "objective": "Build a company dataset",
            "budget_usd": 5,
            "max_cycles": 2,
            "max_research_cells": 10,
        },
    )
    for _ in range(300):
        state = client.get("/api/curator").json()
        if state["status"] == "awaiting_approval":
            break
        time.sleep(0.01)
    client.post("/api/curator/approve")
    for _ in range(300):
        state = client.get("/api/curator").json()
        if state["status"] in {"completed", "paused", "stopped"}:
            break
        time.sleep(0.01)

    assert state["status"] == "completed"
    assert state["completed_cycles"] == 2
    assert state["cycles"][0]["status"] == "failed"
    assert state["cycles"][0]["error"] == "temporary source provider failure"
    assert state["cycles"][1]["written"] == 2
    assert "recoverable execution error" in goals[1]
    assert "temporary source provider failure" in goals[1]
    assert client.get("/api/matrix/Company").json()["rows"][0]["name"] == "Acme"


def test_web_matrix_resolves_references_and_preserves_derivations(tmp_path: Path) -> None:
    root = tmp_path / "project"
    database = LedgerDatabase(root, create=True, name="Relationships")
    database.define_kind("Company")
    database.define_kind("Person")
    company = database.create_entity("Company", "Acme")["entity_id"]
    founder = database.create_entity("Person", "Ada")["entity_id"]
    founder_question = database.define_question("founder", "Company", "Ref[Person]")["question_id"]
    employees = database.define_question("employees", "Company", "Int")["question_id"]
    doubled = database.define_question("employees_doubled", "Company", "Int")["question_id"]
    evidence = database.capture_evidence(
        source_title="Acme profile",
        source_type="web",
        url="https://example.test/acme",
        retrieved_at="2026-11-21",
        excerpt="Ada founded Acme, which has 12 employees.",
    )["evidence_id"]
    database.assert_claim(
        company,
        founder_question,
        founder,
        valid_from="2026-11-21",
        evidence_ids=[evidence],
    )
    input_claim = database.assert_claim(
        company,
        employees,
        12,
        valid_from="2026-11-21",
        evidence_ids=[evidence],
    )["claim_id"]
    database.derive_claim(
        company,
        doubled,
        24,
        valid_from="2026-11-21",
        evidence_ids=[evidence],
        operation="linear",
        input_claim_ids=[input_claim],
        parameters={"multiplier": 2},
    )

    client = TestClient(create_ledger_app(root))
    row = client.get("/api/matrix/Company").json()["rows"][0]
    assert row["cells"]["founder"]["references"] == [
        {"entity_id": founder, "kind": "Person", "name": "Ada"}
    ]
    assert _web_reference({"name": "Grace Hopper", "title": "Technical advisor"}, {}, "Person") == {
        "entity_id": "",
        "kind": "Person",
        "name": "Grace Hopper",
    }
    derivation = row["cells"]["employees_doubled"]["lineage"][0]["derivation"]
    assert derivation["operation"] == "linear"
    assert derivation["dependencies"] == [{"claim_id": input_claim, "role": "operand"}]
    graph = client.get(f"/api/related/{company}").json()
    assert graph["entity"]["name"] == "Acme"
    assert graph["edges"] == [
        {
            "question": "founder",
            "claim_ids": [row["cells"]["founder"]["claims"][0]["claim_id"]],
            "from": {"entity_id": company, "name": "Acme", "kind": "Company"},
            "to": {"entity_id": founder, "name": "Ada", "kind": "Person"},
            "direction": "outgoing",
            "depth": 1,
            "path_claim_ids": [row["cells"]["founder"]["claims"][0]["claim_id"]],
        }
    ]
