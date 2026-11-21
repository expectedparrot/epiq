from pathlib import Path

from fastapi.testclient import TestClient

from epiq.ledger_web import create_ledger_app


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
