import time
from pathlib import Path

from fastapi.testclient import TestClient

from epiq.ledger_api import LedgerDatabase
from epiq.ledger_web import _web_reference, create_ledger_app


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
    assert state["completed_cycles"] == 3
    assert state["cycles"][0]["written"] == 2
    assert state["cycles"][1]["written"] == 0
    assert state["cycles"][2]["written"] == 0
    assert "no useful changes" in state["message"]
    assert state["estimated_spend_usd"] == 3.0
    assert state["budget_usd"] == 10
    assert len(goals) == 3
    assert "Previous cycle outcome" in goals[1]
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
    assert state["completed_cycles"] == 5
    assert state["estimated_spend_usd"] == 4.5
    assert "Focus next on funding" in goals[-1]

    restarted = TestClient(create_ledger_app(root, workspace_agent_runner=curator_plan))
    persisted = restarted.get("/api/curator").json()
    assert persisted["status"] == "completed"
    assert persisted["objective"] == "Build a useful company comparison dataset"


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
