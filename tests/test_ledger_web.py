import time
from pathlib import Path

from fastapi.testclient import TestClient

from epiq.ledger_api import LedgerDatabase
from epiq.ledger_web import create_ledger_app


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
    entity = client.post(
        "/api/entities", json={"kind": "Company", "name": "Acme"}
    ).json()["entity_id"]
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
    assert overview["entity_kinds"] == [
        {"kind": "Company", "entities": 1, "questions": 1}
    ]
    cell = client.get("/api/matrix/Company").json()["rows"][0]["cells"]["employees"]
    assert cell["state"] == "Answered"
    assert cell["value"] == 12
    assert client.get("/api/claims-long").json()["claims"][0]["claim_id"] == claim

    (root / ".epiq" / "projection.sqlite").unlink()
    restarted = TestClient(create_ledger_app(root))
    assert restarted.get("/api/matrix/Company").json()["rows"][0]["cells"]["employees"][
        "value"
    ] == 12
    assert restarted.get("/api/doctor").json()["healthy"] is True


def test_uninitialized_ledger_web_uses_frontend_compatible_error(tmp_path: Path) -> None:
    client = TestClient(create_ledger_app(tmp_path / "missing"))
    response = client.get("/api/project")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "project_not_found"


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

    opened = client.post(
        "/api/projects/open", json={"project_id": project["project_id"]}
    )
    assert opened.status_code == 200
    assert opened.json()["active"] is True
    assert client.get("/api/project").json()["project"]["name"] == "Closeable"


def test_ledger_web_review_visibility_and_retraction_are_durable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    client = TestClient(create_ledger_app(root))
    client.post("/api/project", json={"name": "Review"})
    client.post("/api/entity-kinds", json={"kind": "Item"})
    entity = client.post(
        "/api/entities", json={"kind": "Item", "name": "One"}
    ).json()["entity_id"]
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

    assert client.post(
        f"/api/evidence/{evidence}/assess",
        json={"status": "accepted", "reason": "Checked"},
    ).status_code == 201
    assert client.post(
        f"/api/claims/{claim}/review", json={"reason": "Checked"}
    ).status_code == 200
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
    assert client.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"][
        "state"
    ] == "Unasked"

    (root / ".epiq" / "projection.sqlite").unlink()
    restarted = TestClient(create_ledger_app(root))
    assert restarted.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"][
        "state"
    ] == "Unasked"


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
    entity = client.post(
        "/api/entities", json={"kind": "Item", "name": "One"}
    ).json()["entity_id"]
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
    assert restarted.get("/api/matrix/Item").json()["rows"][0]["cells"]["value"][
        "state"
    ] == "NotFound"


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

    launched = client.post(
        "/api/workspace-agent/jobs", json={"message": "Add photos"}
    )
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
    matrix = client.get("/api/matrix/Faculty").json()
    assert [row["name"] for row in matrix["rows"]] == ["Ada Lovelace"]
    assert matrix["questions"][0]["value_type"] == "Image"
    assert matrix["rows"][0]["cells"]["profile_image"]["value"] == (
        "https://images.example/ada.jpg"
    )

    proposals = LedgerDatabase(root).proposals()
    assert proposals[0]["proposal_type"] == "workspace_agent"


def test_web_matrix_resolves_references_and_preserves_derivations(tmp_path: Path) -> None:
    root = tmp_path / "project"
    database = LedgerDatabase(root, create=True, name="Relationships")
    database.define_kind("Company")
    database.define_kind("Person")
    company = database.create_entity("Company", "Acme")["entity_id"]
    founder = database.create_entity("Person", "Ada")["entity_id"]
    founder_question = database.define_question(
        "founder", "Company", "Ref[Person]"
    )["question_id"]
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

    row = TestClient(create_ledger_app(root)).get("/api/matrix/Company").json()["rows"][0]
    assert row["cells"]["founder"]["references"] == [
        {"entity_id": founder, "kind": "Person", "name": "Ada"}
    ]
    derivation = row["cells"]["employees_doubled"]["lineage"][0]["derivation"]
    assert derivation["operation"] == "linear"
    assert derivation["dependencies"] == [{"claim_id": input_claim, "role": "operand"}]
