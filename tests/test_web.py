from pathlib import Path
from threading import Event
from time import monotonic, sleep

from fastapi.testclient import TestClient

from epiq.store import Store
from epiq.web import create_app


def wait_for_job(client: TestClient, job_id: str) -> dict:
    deadline = monotonic() + 2
    while monotonic() < deadline:
        job = next(
            item for item in client.get("/api/research/jobs").json() if item["job_id"] == job_id
        )
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        sleep(0.01)
    raise AssertionError("Research job did not finish")


def test_research_jobs_can_be_cancelled_without_writing_late_results(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    def research(_kind, _question, targets, _progress=None):
        started.set()
        assert release.wait(1)
        return [
            {
                "entity_id": targets[0]["entity_id"],
                "status": "answered",
                "value": True,
                "confidence": "high",
                "source_title": "Late source",
                "source_url": "https://example.test/late",
                "excerpt": "This result arrived after cancellation.",
            }
        ]

    client = TestClient(
        create_app(tmp_path / "cancel.sqlite", tmp_path / "missing-frontend", research)
    )
    client.post("/api/project", json={"name": "Cancellation"})
    entity = client.post("/api/entities", json={"kind": "Person", "name": "Ada"}).json()
    question = client.post(
        "/api/questions",
        json={"name": "is_founder", "subject_kind": "Person", "value_type": "Bool"},
    ).json()
    launched = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Person",
            "question": question["question_id"],
            "entity_ids": [entity["entity_id"]],
            "scope": "cell",
        },
    ).json()
    assert started.wait(1)
    cancelled = client.post(f"/api/research/jobs/{launched['job_id']}/cancel")
    assert cancelled.status_code == 200
    release.set()
    job = wait_for_job(client, launched["job_id"])
    assert job["status"] == "cancelled"
    assert (
        client.get("/api/matrix/Person").json()["rows"][0]["cells"]["is_founder"]["state"]
        == "Unasked"
    )

    retried = client.post(f"/api/research/jobs/{launched['job_id']}/retry")
    assert retried.status_code == 202
    assert retried.json()["job_id"] != launched["job_id"]
    assert wait_for_job(client, retried.json()["job_id"])["status"] == "completed"


def test_entity_suggestions_are_provisional_until_accepted(tmp_path: Path) -> None:
    def suggest(_kind, existing, count, instructions, progress=None):
        assert [item["name"] for item in existing] == ["Ada"]
        assert count == 3
        assert instructions == "Seed investors"
        if progress:
            progress("Fake candidate search completed")
        return [
            {
                "name": "Ada",
                "rationale": "Already present",
                "source_title": "Ada",
                "source_url": "https://example.test/ada",
            },
            {
                "name": "Grace",
                "rationale": "Invests in early-stage technology companies.",
                "source_title": "Grace profile",
                "source_url": "https://example.test/grace",
            },
        ]

    client = TestClient(
        create_app(
            tmp_path / "web.sqlite",
            tmp_path / "missing-frontend",
            suggestion_runner=suggest,
        )
    )
    client.post("/api/project", json={"name": "Investors"})
    client.post("/api/entities", json={"kind": "Investor", "name": "Ada"})
    launched = client.post(
        "/api/entity-suggestions/jobs",
        json={"entity_kind": "Investor", "count": 3, "instructions": "Seed investors"},
    )
    assert launched.status_code == 202
    job = wait_for_job(client, launched.json()["job_id"])
    assert [item["name"] for item in job["suggestions"]] == ["Grace"]
    assert [row["name"] for row in client.get("/api/matrix/Investor").json()["rows"]] == ["Ada"]
    suggestion = job["suggestions"][0]
    accepted = client.post(
        f"/api/entity-suggestions/{job['job_id']}/accept",
        json={"suggestion_id": suggestion["suggestion_id"]},
    )
    assert accepted.status_code == 201
    assert accepted.json()["status"] == "accepted"
    rows = client.get("/api/matrix/Investor").json()["rows"]
    assert [row["name"] for row in rows] == ["Ada", "Grace"]
    assert rows[1]["attributes"]["suggestion_source_url"] == "https://example.test/grace"


def test_field_suggestions_are_typed_provisional_and_bulk_accepted(tmp_path: Path) -> None:
    def suggest_fields(kind, existing, sample_entities, count, instructions, progress=None):
        assert kind == "Investor"
        assert [item["name"] for item in existing] == ["has_mba"]
        assert [item["name"] for item in sample_entities] == ["Ada"]
        assert count == 3
        assert instructions == "Investment strategy"
        if progress:
            progress("Fake field design completed")
        return [
            {
                "name": "has_mba",
                "label": "Duplicate",
                "value_type": "Bool",
                "rationale": "Already exists",
                "research_guidance": "",
            },
            {
                "name": "typical_check_size",
                "label": "Typical check size",
                "value_type": "Int",
                "rationale": "Makes investment strategy comparable.",
                "research_guidance": "Use the investor's stated typical initial check in USD.",
            },
            {
                "name": "bad type name",
                "label": "Invalid",
                "value_type": "Money",
                "rationale": "Invalid suggestion",
                "research_guidance": "",
            },
        ]

    client = TestClient(
        create_app(
            tmp_path / "fields.sqlite",
            tmp_path / "missing-frontend",
            field_suggestion_runner=suggest_fields,
        )
    )
    client.post("/api/project", json={"name": "Investors"})
    client.post("/api/entities", json={"kind": "Investor", "name": "Ada"})
    client.post(
        "/api/questions",
        json={"name": "has_mba", "subject_kind": "Investor", "value_type": "Bool"},
    )
    launched = client.post(
        "/api/field-suggestions/jobs",
        json={
            "entity_kind": "Investor",
            "count": 3,
            "instructions": "Investment strategy",
        },
    )
    assert launched.status_code == 202
    job = wait_for_job(client, launched.json()["job_id"])
    assert [item["name"] for item in job["field_suggestions"]] == ["typical_check_size"]
    assert [item["name"] for item in client.get("/api/matrix/Investor").json()["questions"]] == [
        "has_mba"
    ]
    accepted = client.post(
        f"/api/field-suggestions/{job['job_id']}/accept",
        json={"suggestion_ids": [job["field_suggestions"][0]["suggestion_id"]]},
    )
    assert accepted.status_code == 201
    questions = client.get("/api/matrix/Investor").json()["questions"]
    assert [item["name"] for item in questions] == ["has_mba", "typical_check_size"]
    assert questions[1]["value_type"] == "Int"
    assert questions[1]["definition"]["suggested_by"] == "agent"


def test_completed_agent_jobs_survive_server_restart(tmp_path: Path) -> None:
    def suggest(_kind, _existing, _count, _instructions, progress=None):
        return [
            {
                "name": "Grace",
                "rationale": "Relevant",
                "source_title": "Profile",
                "source_url": "https://example.test/grace",
            }
        ]

    database = tmp_path / "persistent-jobs.sqlite"
    first = TestClient(
        create_app(database, tmp_path / "missing-frontend", suggestion_runner=suggest)
    )
    first.post("/api/project", json={"name": "Investors"})
    first.post("/api/entities", json={"kind": "Investor", "name": "Ada"})
    launched = first.post(
        "/api/entity-suggestions/jobs", json={"entity_kind": "Investor", "count": 1}
    ).json()
    completed = wait_for_job(first, launched["job_id"])

    restarted = TestClient(create_app(database, tmp_path / "missing-frontend"))
    restored = restarted.get("/api/research/jobs").json()
    assert restored[0]["job_id"] == completed["job_id"]
    assert restored[0]["status"] == "completed"
    assert restored[0]["suggestions"][0]["name"] == "Grace"


def test_interrupted_agent_job_becomes_explicit_failure_on_restart(tmp_path: Path) -> None:
    database = tmp_path / "interrupted.sqlite"
    project = Store(database)
    project.initialize("Interrupted")
    project.save_agent_job(
        {
            "job_id": "job_interrupted",
            "created_at": "2026-08-16T12:00:00Z",
            "status": "running",
            "messages": [],
        }
    )

    client = TestClient(create_app(database, tmp_path / "missing-frontend"))
    job = client.get("/api/research/jobs").json()[0]
    assert job["status"] == "failed"
    assert "server stopped" in job["error"].lower()
    assert Store(database).agent_jobs()[0]["status"] == "failed"


def test_health_doctor_and_online_backup_endpoints(tmp_path: Path) -> None:
    database = tmp_path / "operational.sqlite"
    client = TestClient(create_app(database, tmp_path / "missing-frontend"))
    assert client.get("/api/health").json()["project"] == "uninitialized"
    client.post("/api/project", json={"name": "Operational test"})

    health = client.get("/api/health").json()
    assert health["project"] == "ready"
    assert health["schema_version"] == "13"
    assert client.get("/api/doctor").json()["ok"] is True

    backup = client.get("/api/export/project.sqlite")
    assert backup.status_code == 200
    restored = tmp_path / "restored.sqlite"
    restored.write_bytes(backup.content)
    assert Store(restored).overview()["project"]["name"] == "Operational test"


def test_schema_revisions_preview_compatibility_and_preserve_answers(tmp_path: Path) -> None:
    database = tmp_path / "schema-revision.sqlite"
    project = Store(database)
    project.initialize("Schema revision")
    company = project.add_entity("Company", "Acorn", {}, "test")
    question = project.add_question("employees", "Company", "Int", {"label": "Employees"}, "test")
    _, evidence = project.add_evidence(
        "https://example.test/acorn",
        "Acorn",
        "2026-08-17",
        "Acorn employs 42 people.",
        "test",
    )
    project.assert_claim(company, question, 42, "2026-08-17", evidence, "test")
    client = TestClient(create_app(database, tmp_path / "missing-frontend"))

    body = {
        "value_type": "Float",
        "definition": {"label": "Employee estimate", "cardinality": "one"},
        "reason": "Represent estimates with decimals",
    }
    preview = client.post(f"/api/questions/{question}/revision-preview", json=body)
    assert preview.status_code == 200
    assert preview.json()["can_apply"] is True
    assert preview.json()["checked_values"] == 1

    revised = client.post(f"/api/questions/{question}/revise", json=body)
    assert revised.status_code == 201
    matrix = client.get("/api/matrix/Company").json()
    assert matrix["questions"][0]["value_type"] == "Float"
    assert matrix["questions"][0]["definition"]["label"] == "Employee estimate"
    assert matrix["rows"][0]["cells"]["employees"]["value"] == 42

    incompatible = {
        **body,
        "value_type": "URL",
        "reason": "Incorrect attempted conversion",
    }
    rejected_preview = client.post(
        f"/api/questions/{revised.json()['question_id']}/revision-preview",
        json=incompatible,
    ).json()
    assert rejected_preview["can_apply"] is False
    rejected = client.post(
        f"/api/questions/{revised.json()['question_id']}/revise", json=incompatible
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "incompatible_schema_revision"


def test_agent_discovery_diagnostics_and_derivation_api(tmp_path: Path) -> None:
    database = tmp_path / "agent-api.sqlite"
    uninitialized = TestClient(create_app(database, tmp_path / "missing-frontend"))
    discovery = uninitialized.get("/api/capabilities", params={"command": "record"})
    assert discovery.status_code == 200
    assert discovery.json()["commands"][0]["name"] == "record"

    project = Store(database)
    project.initialize("Agent API")
    project.add_entity("Quote", "Atlas", {}, "test")
    project.add_question("price", "Quote", "Float", {}, "test")
    project.add_question("shipping", "Quote", "Float", {}, "test")
    project.add_question(
        "total",
        "Quote",
        "Float",
        {"formula": {"operation": "sum", "inputs": ["price", "shipping"]}},
        "test",
    )
    _, evidence = project.add_evidence(
        "urn:test:quote", "Quote", "2026-08-17", "Price 40, shipping 5", "test"
    )
    project.assert_claim("Atlas", "price", 40.0, "2026-08-17", evidence, "test")
    project.assert_claim("Atlas", "shipping", 5.0, "2026-08-17", evidence, "test")

    client = TestClient(create_app(database, tmp_path / "missing-frontend"))
    schema = client.get("/api/schema").json()
    assert [item["name"] for item in schema["tables"][0]["questions"]] == [
        "price",
        "shipping",
        "total",
    ]
    assert client.get("/api/context", params={"budget": 1000}).json()["truncated"] is False
    assert client.get("/api/gaps/Quote").json()["count"] == 1
    assert client.get("/api/refresh-plan/Quote").json()["tasks"][0]["question"] == "total"
    aggregate = client.post(
        "/api/aggregate/Quote", json={"question": "price", "operation": "avg"}
    ).json()
    assert aggregate["groups"] == [{"group": "all", "value": 40.0, "count": 1}]

    materialized = client.post(
        "/api/materialize",
        json={"entity_kind": "Quote", "valid_from": "2026-08-17"},
    )
    assert materialized.status_code == 201
    assert materialized.json()["results"][0]["status"] == "materialized"
    assert client.get("/api/stale-derivations").json()["count"] == 0

    _, newer_evidence = project.add_evidence(
        "urn:test:quote-update", "Update", "2026-08-18", "Price 42", "test"
    )
    project.assert_claim("Atlas", "price", 42.0, "2026-08-18", newer_evidence, "test")
    assert client.get("/api/stale-derivations").json()["count"] == 1
    assert client.get("/api/search", params={"text": "shipping"}).json()["count"] >= 1


def test_web_can_retire_and_restore_a_field_without_erasing_history(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "retire.sqlite", tmp_path / "missing-frontend"))
    client.post("/api/project", json={"name": "Field lifecycle"})
    client.post("/api/entities", json={"kind": "Office", "name": "Example"})
    question = client.post(
        "/api/questions",
        json={"name": "rating", "subject_kind": "Office", "value_type": "Float"},
    ).json()["question_id"]

    retired = client.post(f"/api/questions/{question}/retire", json={"reason": "Wrong semantics"})
    assert retired.status_code == 200
    assert client.get("/api/matrix/Office").json()["questions"] == []
    history = client.get("/api/history?event_type=question.retire").json()
    assert history[0]["payload"]["reason"] == "Wrong semantics"

    restored = client.post(
        f"/api/questions/{question}/restore", json={"reason": "Redesigned and useful"}
    )
    assert restored.status_code == 200
    assert client.get("/api/matrix/Office").json()["questions"][0]["name"] == "rating"


def test_whole_table_research_launches_each_question_with_gaps(tmp_path: Path) -> None:
    def researcher(_kind, question, entities, progress=None):
        return [
            {
                "entity_id": entity["entity_id"],
                "status": "not_found",
                "value": None,
                "source_type": "web",
                "source_url": None,
                "source_title": "",
                "excerpt": "",
                "confidence": "low",
                "notes": f"No evidence for {question['name']}",
            }
            for entity in entities
        ]

    client = TestClient(
        create_app(tmp_path / "table.sqlite", tmp_path / "missing-frontend", researcher)
    )
    client.post("/api/project", json={"name": "Investors"})
    client.post("/api/entities", json={"kind": "Investor", "name": "Ada"})
    for name in ("has_mba", "lives_in_ca"):
        client.post(
            "/api/questions",
            json={"name": name, "subject_kind": "Investor", "value_type": "Bool"},
        )
    launched = client.post("/api/research/table", json={"entity_kind": "Investor"})
    assert launched.status_code == 202
    assert launched.json()["questions"] == 2
    assert len(launched.json()["jobs"]) == 2
    for job in launched.json()["jobs"]:
        assert wait_for_job(client, job["job_id"])["status"] == "completed"


def test_claim_challenge_records_feedback_retracts_and_versions_guidance(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "challenge.sqlite", tmp_path / "missing"))
    client.post("/api/project", json={"name": "People"})
    entity = client.post("/api/entities", json={"kind": "Person", "name": "Example"}).json()[
        "entity_id"
    ]
    question = client.post(
        "/api/questions",
        json={"name": "citizen_at_birth", "subject_kind": "Person", "value_type": "Bool"},
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "url": "https://example.test/bio",
            "title": "Biography",
            "retrieved_at": "2026-08-16",
            "excerpt": "Born outside the United States.",
        },
    ).json()["evidence_id"]
    claim = client.post(
        "/api/claims",
        json={
            "subject": entity,
            "question": question,
            "value": False,
            "valid_from": "2026-08-16",
            "evidence_ids": [evidence],
        },
    ).json()["claim_id"]
    challenged = client.post(
        f"/api/claims/{claim}/challenge",
        json={
            "reason": "Birthplace alone does not determine citizenship at birth.",
            "research_guidance": "Check parental citizenship and the law in effect at birth.",
            "retract": True,
        },
    )
    assert challenged.status_code == 201
    assert challenged.json()["status"] == "retracted"
    matrix = client.get("/api/matrix/Person").json()
    assert matrix["rows"][0]["cells"]["citizen_at_birth"]["state"] == "Unasked"
    assert (
        matrix["questions"][0]["definition"]["research_guidance"]
        == "Check parental citizenship and the law in effect at birth."
    )
    feedback = client.get("/api/history", params={"event_type": "claim.feedback"}).json()
    assert feedback[0]["payload"]["claim_id"] == claim


def test_matrix_keeps_fields_in_creation_order_across_new_versions(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "order.sqlite", tmp_path / "missing"))
    client.post("/api/project", json={"name": "People"})
    client.post("/api/entities", json={"kind": "Person", "name": "Example"})
    first = client.post(
        "/api/questions",
        json={"name": "z_first", "subject_kind": "Person", "value_type": "String"},
    ).json()["question_id"]
    client.post(
        "/api/questions",
        json={"name": "a_second", "subject_kind": "Person", "value_type": "String"},
    )
    assert [item["name"] for item in client.get("/api/matrix/Person").json()["questions"]] == [
        "z_first",
        "a_second",
    ]
    client.post(
        f"/api/questions/{first}/policy",
        json={"volatility": "dynamic", "freshness_days": 30},
    )
    questions = client.get("/api/matrix/Person").json()["questions"]
    assert [item["name"] for item in questions] == ["z_first", "a_second"]
    assert questions[0]["question_id"] == "q_z_first_v2"


def test_question_challenge_http_api_is_review_first(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "boats.sqlite", tmp_path / "missing"))
    client.post("/api/project", json={"name": "Boats"})
    boat = client.post("/api/entities", json={"kind": "BoatModel", "name": "RS Quest"}).json()[
        "entity_id"
    ]
    question = client.post(
        "/api/questions",
        json={"name": "has_spinnaker", "subject_kind": "BoatModel", "value_type": "Bool"},
    ).json()["question_id"]
    created = client.post(
        f"/api/questions/{question}/challenges",
        json={
            "problem": "modal_ambiguity",
            "explanation": "Optional capability is not actual possession.",
            "example_entity": boat,
            "proposed_replacement": {
                "name": "spinnaker_availability",
                "value_type": "Enum[standard,optional,unavailable]",
            },
        },
    )
    assert created.status_code == 201
    challenge_id = created.json()["challenge_id"]
    assert (
        client.get(
            "/api/question-challenges", params={"question": "has_spinnaker", "status": "open"}
        ).json()[0]["challenge_id"]
        == challenge_id
    )
    resolved = client.post(
        f"/api/question-challenges/{challenge_id}/resolve",
        json={"status": "resolved", "resolution": "Split capability from equipment."},
    )
    assert resolved.json()["status"] == "resolved"


def test_project_lifecycle_and_excel_download(tmp_path: Path) -> None:
    database = tmp_path / "current.sqlite"
    client = TestClient(
        create_app(
            database,
            tmp_path / "missing",
            projects_directory=tmp_path / "projects",
        )
    )
    client.post("/api/project", json={"name": "Current research"})
    original = client.get("/api/projects").json()
    assert [item["name"] for item in original] == ["Current research"]
    created = client.post("/api/projects", json={"name": "New Market"})
    assert created.status_code == 201
    assert created.json()["active"] is True
    client.post("/api/entity-kinds", json={"kind": "Company"})
    workbook = client.get("/api/export/Company.xlsx")
    assert workbook.status_code == 200
    assert workbook.content.startswith(b"PK")
    assert "Company.xlsx" in workbook.headers["content-disposition"]
    assert client.post("/api/projects/close", json={}).json() == {"closed": True}
    inactive = client.get("/api/project")
    assert inactive.status_code == 400
    assert inactive.json()["error"]["code"] == "no_active_project"
    projects = client.get("/api/projects").json()
    original_id = next(
        item["project_id"] for item in projects if item["name"] == "Current research"
    )
    opened = client.post("/api/projects/open", json={"project_id": original_id})
    assert opened.status_code == 200
    assert client.get("/api/project").json()["project"]["name"] == "Current research"


def test_web_api_supports_the_spreadsheet_claim_lifecycle(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "web.sqlite", tmp_path / "missing-frontend"))

    health = client.get("/api/health").json()
    assert health["initialized"] is False
    missing = client.get("/api/project")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "project_not_found"

    assert client.post("/api/project", json={"name": "Market"}).status_code == 201
    entity = client.post(
        "/api/entities", json={"kind": "Company", "name": "Example", "attributes": {}}
    ).json()["entity_id"]
    question = client.post(
        "/api/questions",
        json={
            "name": "status",
            "subject_kind": "Company",
            "value_type": "String",
            "definition": {"label": "Status"},
        },
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "url": "https://example.test/about",
            "title": "About",
            "retrieved_at": "2026-08-16",
            "excerpt": "The company is active.",
        },
    ).json()["evidence_id"]
    claim_response = client.post(
        "/api/claims",
        json={
            "subject": entity,
            "question": question,
            "value": "active",
            "valid_from": "2026-08-16",
            "evidence_ids": [evidence],
            "confidence": "high",
        },
    )
    assert claim_response.status_code == 201
    claim = claim_response.json()["claim_id"]

    cell = client.get("/api/matrix/Company").json()["rows"][0]["cells"]["status"]
    assert cell["state"] == "Answered"
    assert cell["value"] == "active"
    assert cell["lineage"][0]["source"]["url"] == "https://example.test/about"

    assert (
        client.post(
            f"/api/claims/{claim}/retract", json={"reason": "Superseded by new research"}
        ).json()["status"]
        == "retracted"
    )
    retracted_cell = client.get("/api/matrix/Company").json()["rows"][0]["cells"]["status"]
    assert retracted_cell["state"] == "Unasked"
    assert len(client.get("/api/history", params={"event_type": "claim.retracted"}).json()) == 1


def test_web_api_exposes_contested_and_not_found_cells(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "web.sqlite", tmp_path / "missing-frontend"))
    client.post("/api/project", json={"name": "Market"})
    entity = client.post("/api/entities", json={"kind": "Company", "name": "Example"}).json()[
        "entity_id"
    ]
    client.post(
        "/api/questions",
        json={"name": "status", "subject_kind": "Company", "value_type": "String"},
    )
    client.post(
        "/api/questions",
        json={"name": "pricing", "subject_kind": "Company", "value_type": "String"},
    )
    for index, value in enumerate(["active", "closed"]):
        evidence = client.post(
            "/api/evidence",
            json={
                "url": f"https://example.test/{index}",
                "title": f"Source {index}",
                "retrieved_at": "2026-08-16",
                "excerpt": value,
            },
        ).json()["evidence_id"]
        client.post(
            "/api/claims",
            json={
                "subject": entity,
                "question": "status",
                "value": value,
                "valid_from": "2026-08-16",
                "evidence_ids": [evidence],
            },
        )
    client.post(
        "/api/research/not-found",
        json={
            "subject": entity,
            "question": "pricing",
            "query": "site:example.test pricing",
            "notes": "No public price found.",
        },
    )

    cells = client.get("/api/matrix/Company").json()["rows"][0]["cells"]
    assert cells["status"]["state"] == "Contested"
    assert cells["pricing"]["state"] == "NotFound"


def test_not_found_feedback_can_retry_as_an_evidence_backed_negative(tmp_path: Path) -> None:
    def researcher(_kind, question, entities, progress=None):
        assert question["task_mode"] == "retry_not_found"
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "answered",
                "value": False,
                "source_type": "web",
                "source_url": "https://example.test/complete-recipients",
                "source_title": "Complete recipient list",
                "source_published_at": "2026-01-01",
                "observed_as_of": "2026-01-01",
                "excerpt": "Complete list of all recipients through 2026.",
                "confidence": "high",
                "notes": "The person is absent from the exhaustive list.",
            }
        ]

    client = TestClient(create_app(tmp_path / "retry.sqlite", tmp_path / "missing", researcher))
    client.post("/api/project", json={"name": "People"})
    person = client.post("/api/entities", json={"kind": "Person", "name": "Example"}).json()[
        "entity_id"
    ]
    question = client.post(
        "/api/questions",
        json={"name": "won_medal", "subject_kind": "Person", "value_type": "Bool"},
    ).json()["question_id"]
    task = client.post(
        "/api/research/not-found",
        json={
            "subject": person,
            "question": question,
            "query": "official recipients",
            "notes": "Person absent from the official complete list.",
        },
    ).json()["task_id"]
    feedback = client.post(
        f"/api/research/{task}/feedback",
        json={
            "reason": "The list is exhaustive, so absence is probative.",
            "research_guidance": "Use an authoritative exhaustive list as negative evidence.",
            "save_to_field": True,
        },
    )
    assert feedback.status_code == 201
    assert feedback.json()["question_id"] == "q_won_medal_v2"
    launched = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Person",
            "question": feedback.json()["question_id"],
            "mode": "retry_not_found",
            "entity_ids": [person],
            "scope": "cell",
        },
    ).json()
    completed_job = wait_for_job(client, launched["job_id"])
    assert completed_job["status"] == "completed", completed_job["error"]
    cell = client.get("/api/matrix/Person").json()["rows"][0]["cells"]["won_medal"]
    assert cell["state"] == "Answered"
    assert cell["value"] is False
    assert cell["lineage"][0]["source"]["title"] == "Complete recipient list"


def test_web_api_enforces_enum_question_choices(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "enum.sqlite", tmp_path / "missing"))
    client.post("/api/project", json={"name": "Boats"})
    boat = client.post("/api/entities", json={"kind": "BoatModel", "name": "RS Quest"}).json()[
        "entity_id"
    ]
    question = client.post(
        "/api/questions",
        json={
            "name": "spinnaker_availability",
            "subject_kind": "BoatModel",
            "value_type": "Enum[standard,optional,unavailable,unknown]",
        },
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "url": "https://example.test/quest",
            "title": "Options",
            "retrieved_at": "2026-08-16",
            "excerpt": "An asymmetric spinnaker is optional.",
        },
    ).json()["evidence_id"]
    accepted = client.post(
        "/api/claims",
        json={
            "subject": boat,
            "question": question,
            "value": "optional",
            "valid_from": "2026-08-16",
            "evidence_ids": [evidence],
        },
    )
    assert accepted.status_code == 201
    rejected = client.post(
        "/api/claims",
        json={
            "subject": boat,
            "question": question,
            "value": "sometimes",
            "valid_from": "2026-08-16",
            "evidence_ids": [evidence],
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "value_type_error"


def test_web_app_serves_built_assets_and_spa_fallback(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<main>Epiq application</main>")
    (frontend / "assets" / "app.js").write_text("console.log('epiq')")
    client = TestClient(create_app(tmp_path / "web.sqlite", frontend))

    assert client.get("/").text == "<main>Epiq application</main>"
    assert client.get("/tables/Company").text == "<main>Epiq application</main>"
    assert client.get("/assets/app.js").text == "console.log('epiq')"


def test_web_api_creates_empty_sheet_and_accepts_non_url_evidence(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "web.sqlite", tmp_path / "missing-frontend"))
    client.post("/api/project", json={"name": "People I know"})

    created = client.post("/api/entity-kinds", json={"kind": "Investor"})
    assert created.status_code == 201
    assert created.json() == {"kind": "Investor"}
    assert client.get("/api/project").json()["entity_kinds"] == [
        {"kind": "Investor", "entities": 0, "questions": 0}
    ]

    personal = client.post(
        "/api/evidence",
        json={
            "source_type": "personal",
            "title": "Personal knowledge",
            "retrieved_at": "2026-08-16",
            "excerpt": "He told me directly during a conversation.",
        },
    )
    assert personal.status_code == 201

    missing_web_url = client.post(
        "/api/evidence",
        json={
            "source_type": "web",
            "title": "Website",
            "retrieved_at": "2026-08-16",
            "excerpt": "A web assertion.",
        },
    )
    assert missing_web_url.status_code == 400
    assert missing_web_url.json()["error"]["code"] == "source_url_required"


def test_background_research_job_fills_only_unasked_cells(tmp_path: Path) -> None:
    def researcher(
        _kind: str,
        _question: dict,
        entities: list[dict],
        progress=None,
    ) -> list[dict]:
        if progress:
            progress("Fake search completed")
        if _question.get("instructions") == "No Scholar access":
            return [
                {
                    "entity_id": entity["entity_id"],
                    "status": "not_found",
                    "value": None,
                    "notes": "Google Scholar could not be accessed.",
                }
                for entity in entities
            ]
        return [
            {
                "entity_id": entity["entity_id"],
                "status": "answered",
                "value": entity.get("existing_value", True),
                "source_type": "web",
                "source_url": f"https://example.test/{entity['entity_id']}",
                "source_title": "Biography",
                "excerpt": "The biography confirms the degree.",
                "confidence": "high",
                "notes": "",
            }
            for entity in entities
        ]

    client = TestClient(
        create_app(tmp_path / "web.sqlite", tmp_path / "missing-frontend", researcher)
    )
    client.post("/api/project", json={"name": "Investors"})
    first = client.post("/api/entities", json={"kind": "Investor", "name": "Ada"}).json()[
        "entity_id"
    ]
    second = client.post("/api/entities", json={"kind": "Investor", "name": "Grace"}).json()[
        "entity_id"
    ]
    question = client.post(
        "/api/questions",
        json={"name": "has_mba", "subject_kind": "Investor", "value_type": "Bool"},
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "source_type": "personal",
            "title": "Personal knowledge",
            "retrieved_at": "2026-08-16",
            "excerpt": "Ada told me directly.",
        },
    ).json()["evidence_id"]
    client.post(
        "/api/claims",
        json={
            "subject": first,
            "question": question,
            "value": False,
            "valid_from": "2026-08-16",
            "evidence_ids": [evidence],
        },
    )

    launched = client.post(
        "/api/research/jobs", json={"entity_kind": "Investor", "question": question}
    )
    assert launched.status_code == 202
    job = wait_for_job(client, launched.json()["job_id"])
    assert job["status"] == "completed"
    assert job["outcome"] == "changed"
    assert job["written"] == 1
    assert job["total"] == 1
    assert job["completed"] == 1

    rows = client.get("/api/matrix/Investor").json()["rows"]
    cells = {row["name"]: row["cells"]["has_mba"] for row in rows}
    assert cells["Ada"]["value"] is False
    assert cells["Grace"]["value"] is True
    assert cells["Grace"]["lineage"][0]["source"]["title"] == "Biography"

    enrichment = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Investor",
            "question": question,
            "mode": "add_evidence",
            "instructions": "Prefer university biographies.",
            "entity_ids": [first],
        },
    ).json()
    enriched_job = wait_for_job(client, enrichment["job_id"])
    assert enriched_job["status"] == "completed"
    assert enriched_job["outcome"] == "changed"
    assert enriched_job["total"] == 1
    assert any(
        message["message"] == "Fake search completed" for message in enriched_job["messages"]
    )
    enriched_rows = client.get("/api/matrix/Investor").json()["rows"]
    enriched = {row["name"]: row["cells"]["has_mba"] for row in enriched_rows}
    assert enriched["Ada"]["value"] is False
    assert enriched["Ada"]["state"] == "Answered"
    assert len(enriched["Ada"]["lineage"]) == 2

    duplicate = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Investor",
            "question": question,
            "mode": "add_evidence",
            "entity_ids": [first],
        },
    ).json()
    duplicate_job = wait_for_job(client, duplicate["job_id"])
    assert duplicate_job["outcome"] == "no_change"
    assert duplicate_job["rejected"] == 1
    assert any(
        "Rejected duplicate source" in message["message"] for message in duplicate_job["messages"]
    )
    duplicate_rows = client.get("/api/matrix/Investor").json()["rows"]
    duplicate_cell = next(row for row in duplicate_rows if row["name"] == "Ada")["cells"]["has_mba"]
    assert len(duplicate_cell["lineage"]) == 2

    def conflicting_researcher(_kind, _question, entities, progress=None):
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "answered",
                "value": True,
                "source_type": "web",
                "source_url": "https://semantic-scholar.example/paper",
                "source_title": "Semantic Scholar",
                "excerpt": "This source reports a different value.",
                "confidence": "high",
                "notes": "",
            }
        ]

    client.app.state.research_runner = conflicting_researcher
    conflict = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Investor",
            "question": question,
            "mode": "add_evidence",
            "instructions": "Use Semantic Scholar",
            "entity_ids": [first],
        },
    ).json()
    conflict_job = wait_for_job(client, conflict["job_id"])
    assert conflict_job["outcome"] == "changed"
    assert any("conflicting value" in item["message"] for item in conflict_job["messages"])
    contested = next(
        row for row in client.get("/api/matrix/Investor").json()["rows"] if row["name"] == "Ada"
    )["cells"]["has_mba"]
    assert contested["state"] == "Contested"
    assert set(contested["values"]) == {False, True}

    client.app.state.research_runner = researcher
    no_source = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Investor",
            "question": question,
            "mode": "add_evidence",
            "instructions": "No Scholar access",
            "entity_ids": [second],
        },
    ).json()
    no_source_job = wait_for_job(client, no_source["job_id"])
    assert no_source_job["outcome"] == "no_change"
    assert no_source_job["no_result"] == 1
    assert any(
        "Google Scholar could not be accessed" in message["message"]
        for message in no_source_job["messages"]
    )

    client.post(
        "/api/questions",
        json={"name": "is_founder", "subject_kind": "Investor", "value_type": "Bool"},
    )
    row_launch = client.post(
        "/api/research/rows",
        json={
            "entity_kind": "Investor",
            "entity_id": first,
            "instructions": "Prefer primary sources.",
        },
    )
    assert row_launch.status_code == 202
    row_jobs = row_launch.json()["jobs"]
    assert len(row_jobs) == 1
    row_job = wait_for_job(client, row_jobs[0]["job_id"])
    assert row_job["status"] == "completed"
    assert row_job["requested_entity_ids"] == [first]
    final_rows = client.get("/api/matrix/Investor").json()["rows"]
    final = {row["name"]: row["cells"]["is_founder"] for row in final_rows}
    assert final["Ada"]["value"] is True
    assert final["Grace"]["state"] == "Unasked"


def test_column_research_fans_out_into_independently_completing_cells(
    tmp_path: Path,
) -> None:
    calls = []

    def researcher(_kind, _question, entities, progress=None):
        assert len(entities) == 1
        calls.append(entities[0]["entity_id"])
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "not_found",
                "value": None,
                "source_type": "web",
                "source_url": None,
                "source_title": "",
                "excerpt": "",
                "confidence": "low",
                "notes": "No sufficient source.",
            }
        ]

    client = TestClient(create_app(tmp_path / "column.sqlite", tmp_path / "missing", researcher))
    client.post("/api/project", json={"name": "Investors"})
    for name in ("Ada", "Grace", "Katherine"):
        client.post("/api/entities", json={"kind": "Investor", "name": name})
    question = client.post(
        "/api/questions",
        json={"name": "has_mba", "subject_kind": "Investor", "value_type": "Bool"},
    ).json()["question_id"]
    launched = client.post(
        "/api/research/column",
        json={"entity_kind": "Investor", "question": question, "scope": "column"},
    )
    assert launched.status_code == 202
    jobs = launched.json()["jobs"]
    assert len(jobs) == 3
    assert all(len(job["requested_entity_ids"]) == 1 for job in jobs)
    for job in jobs:
        assert wait_for_job(client, job["job_id"])["status"] == "completed"
    assert len(calls) == 3


def test_claim_proposal_review_and_bulk_api(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "claims.sqlite", tmp_path / "missing"))
    client.post("/api/project", json={"name": "Claims"})
    entity = client.post("/api/entities", json={"kind": "Company", "name": "Acme"}).json()[
        "entity_id"
    ]
    client.post(
        "/api/questions",
        json={"name": "active", "subject_kind": "Company", "value_type": "Bool"},
    )
    evidence = client.post(
        "/api/evidence",
        json={
            "url": "https://example.test/acme",
            "title": "Acme",
            "retrieved_at": "2026-08-17",
            "excerpt": "Acme is active.",
        },
    ).json()["evidence_id"]
    claim = {
        "subject": entity,
        "question": "active",
        "value": True,
        "valid_from": "2026-08-17",
        "evidence_ids": [evidence],
    }
    proposed = client.post("/api/claim-proposals", json={**claim, "rationale": "Official source"})
    assert proposed.status_code == 201
    proposal_id = proposed.json()["proposal_id"]
    assert (
        client.get("/api/matrix/Company").json()["rows"][0]["cells"]["active"]["state"] == "Unasked"
    )
    reviewed = client.post(
        "/api/claim-proposals/review",
        json={
            "proposal_ids": [proposal_id],
            "decision": "approved",
            "reason": "Verified",
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["results"][0]["claim_id"]

    bulk = client.post(
        "/api/claims/bulk",
        json={"claims": [{**claim, "value": False, "valid_from": "2026-08-18"}]},
    )
    assert bulk.status_code == 201
    assert bulk.json()["count"] == 1
