import importlib.util
import sqlite3
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


def test_active_cell_research_launches_are_deduplicated(tmp_path: Path) -> None:
    started = Event()
    release = Event()
    calls = 0

    def researcher(_kind, _question, entities, progress=None):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        if not entities:
            return []
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "not_found",
                "notes": "No source found.",
            }
        ]

    client = TestClient(
        create_app(tmp_path / "deduplicated-research.sqlite", tmp_path / "missing", researcher)
    )
    client.post("/api/project", json={"name": "Deduplicated research"})
    entity = client.post("/api/entities", json={"kind": "Company", "name": "Acme"}).json()[
        "entity_id"
    ]
    client.post("/api/entities", json={"kind": "Company", "name": "Beta"})
    question = client.post(
        "/api/questions",
        json={"name": "founded", "subject_kind": "Company", "value_type": "Year"},
    ).json()["question_id"]
    request = {
        "entity_kind": "Company",
        "question": question,
        "entity_ids": [entity],
        "scope": "cell",
    }
    first = client.post("/api/research/jobs", json=request).json()
    assert started.wait(timeout=1)
    duplicate = client.post("/api/research/jobs", json=request).json()
    assert duplicate["job_id"] == first["job_id"]
    assert duplicate["deduplicated"] is True
    assert len(client.get("/api/research/jobs").json()) == 1
    assert calls == 1
    column = client.post(
        "/api/research/column",
        json={"entity_kind": "Company", "question": question, "scope": "column"},
    ).json()
    assert column["cells"] == 2
    assert sum(bool(job["deduplicated"]) for job in column["jobs"]) == 1
    assert len({job["job_id"] for job in column["jobs"]}) == 2
    assert len(client.get("/api/research/jobs").json()) == 2
    release.set()
    for job in column["jobs"]:
        wait_for_job(client, job["job_id"])

    later = client.post("/api/research/jobs", json=request).json()
    assert later["job_id"] != first["job_id"]
    assert later["deduplicated"] is False


def test_research_receives_project_table_and_entity_identity_context(tmp_path: Path) -> None:
    captured = {}

    def researcher(kind, question, targets, progress=None):
        captured.update({"kind": kind, "question": question, "targets": targets})
        return [
            {
                "entity_id": targets[0]["entity_id"],
                "status": "not_found",
                "value": None,
                "source_type": "web",
                "source_url": None,
                "source_title": "",
                "excerpt": "",
                "confidence": "low",
                "notes": "Identity could not be verified.",
            }
        ]

    client = TestClient(create_app(tmp_path / "context.sqlite", tmp_path / "missing", researcher))
    client.post("/api/project", json={"name": "Cape Cod Towns"})
    orleans = client.post(
        "/api/entities",
        json={
            "kind": "Town",
            "name": "Orleans",
            "attributes": {"region": "Cape Cod, Massachusetts"},
        },
    ).json()["entity_id"]
    client.post("/api/entities", json={"kind": "Town", "name": "Wellfleet"})
    question = client.post(
        "/api/questions",
        json={
            "name": "wikipedia_url",
            "subject_kind": "Town",
            "value_type": "URL",
            "definition": {"label": "Wikipedia URL"},
        },
    ).json()["question_id"]
    job = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Town",
            "question": question,
            "entity_ids": [orleans],
            "scope": "cell",
        },
    ).json()
    wait_for_job(client, job["job_id"])

    assert captured["question"]["research_context"]["project"]["name"] == "Cape Cod Towns"
    peer_names = {
        row["name"] for row in captured["question"]["research_context"]["table"]["peer_rows"]
    }
    assert peer_names == {"Orleans", "Wellfleet"}
    assert captured["targets"][0]["attributes"] == {"region": "Cape Cod, Massachusetts"}


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


def test_column_research_can_be_cancelled_as_one_scope(tmp_path: Path) -> None:
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
                "excerpt": "This result arrived after scoped cancellation.",
            }
        ]

    client = TestClient(
        create_app(tmp_path / "cancel-column.sqlite", tmp_path / "missing", research)
    )
    client.post("/api/project", json={"name": "Column cancellation"})
    for name in ["Ada", "Grace"]:
        client.post("/api/entities", json={"kind": "Person", "name": name})
    question = client.post(
        "/api/questions",
        json={"name": "is_founder", "subject_kind": "Person", "value_type": "Bool"},
    ).json()["question_id"]
    launched = client.post(
        "/api/research/column",
        json={"entity_kind": "Person", "question": question, "scope": "column"},
    ).json()
    assert started.wait(1)
    cancelled = client.post(
        "/api/research/cancel",
        json={"scope": "column", "entity_kind": "Person", "question_id": question},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["count"] == 2
    release.set()
    for job in launched["jobs"]:
        assert wait_for_job(client, job["job_id"])["status"] == "cancelled"
    assert all(
        row["cells"]["is_founder"]["state"] == "Unasked"
        for row in client.get("/api/matrix/Person").json()["rows"]
    )


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


def test_web_api_creates_tables_and_many_relationships(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "relationships.sqlite", tmp_path / "missing"))
    client.post("/api/project", json={"name": "Papers"})
    client.post("/api/entity-kinds", json={"kind": "Paper"})
    client.post("/api/entity-kinds", json={"kind": "Author"})
    paper = client.post("/api/entities", json={"kind": "Paper", "name": "Market Design"}).json()[
        "entity_id"
    ]
    authors = [
        client.post("/api/entities", json={"kind": "Author", "name": name}).json()["entity_id"]
        for name in ["Ada", "Grace"]
    ]
    question = client.post(
        "/api/questions",
        json={
            "name": "authors",
            "subject_kind": "Paper",
            "value_type": "Ref[Author]",
            "definition": {"label": "Authors", "cardinality": "many"},
        },
    ).json()["question_id"]
    evidence = client.post(
        "/api/evidence",
        json={
            "source_type": "report",
            "title": "Paper title page",
            "retrieved_at": "2026-08-17",
            "excerpt": "The title page lists Ada and Grace.",
        },
    ).json()["evidence_id"]
    for author in authors:
        response = client.post(
            "/api/claims",
            json={
                "subject": paper,
                "question": question,
                "value": author,
                "valid_from": "2026-08-17",
                "evidence_ids": [evidence],
            },
        )
        assert response.status_code == 201

    cell = client.get("/api/matrix/Paper").json()["rows"][0]["cells"]["authors"]
    assert cell["state"] == "Answered"
    assert [item["name"] for item in cell["references"]] == ["Grace", "Ada"]
    related = client.get(f"/api/related/{paper}").json()
    assert {item["to"]["name"] for item in related["edges"]} == {"Ada", "Grace"}
    back_references = client.get(
        f"/api/related/{authors[0]}", params={"direction": "incoming"}
    ).json()
    assert back_references["count"] == 1
    assert back_references["edges"][0]["from"]["name"] == "Market Design"
    assert back_references["edges"][0]["question"] == "authors"


def test_duplicate_candidate_scan_proposes_qualified_and_descriptive_names(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "duplicates.sqlite", tmp_path / "missing"))
    client.post("/api/project", json={"name": "Duplicate review"})
    for name in [
        "Gaiety Theatre",
        "Gaiety Theatre (London, England)",
        "Opera Comique",
        "Opera Comique Theatre",
        "Royalty Theatre",
    ]:
        assert client.post("/api/entities", json={"kind": "Theater", "name": name}).status_code == 201

    response = client.get("/api/entities/duplicate-candidates", params={"kind": "Theater"})

    assert response.status_code == 200
    candidates = response.json()["candidates"]
    pairs = {(item["duplicate"]["name"], item["survivor"]["name"]) for item in candidates}
    assert ("Gaiety Theatre", "Gaiety Theatre (London, England)") in pairs
    assert ("Opera Comique", "Opera Comique Theatre") in pairs
    assert all("Royalty Theatre" not in pair for pair in pairs)
    assert all(item["score"] >= 0.92 for item in candidates)


def test_relationship_research_stages_entities_and_links_for_approval(tmp_path: Path) -> None:
    def researcher(_kind, question, entities, progress=None):
        assert question["value_type"] == "Ref[Author]"
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "answered",
                "value": [
                    "Ada",
                    {
                        "name": "Katherine Johnson",
                        "birth_year": 1918,
                        "expertise": ["mathematics", "spaceflight"],
                    },
                ],
                "source_type": "web",
                "source_url": "https://example.test/title-page",
                "source_title": "Paper title page",
                "excerpt": "Written by Ada and Katherine Johnson.",
                "confidence": "high",
                "notes": "",
            }
        ]

    client = TestClient(
        create_app(tmp_path / "relationship-research.sqlite", tmp_path / "missing", researcher)
    )
    client.post("/api/project", json={"name": "Relationship research"})
    paper = client.post(
        "/api/entities", json={"kind": "Paper", "name": "Computing Markets"}
    ).json()["entity_id"]
    ada = client.post("/api/entities", json={"kind": "Author", "name": "Ada"}).json()["entity_id"]
    client.post(
        "/api/questions",
        json={"name": "birth_year", "subject_kind": "Author", "value_type": "Year"},
    )
    client.post(
        "/api/questions",
        json={
            "name": "expertise",
            "subject_kind": "Author",
            "value_type": "String",
            "definition": {"cardinality": "many"},
        },
    )
    question = client.post(
        "/api/questions",
        json={
            "name": "authors",
            "subject_kind": "Paper",
            "value_type": "Ref[Author]",
            "definition": {"cardinality": "many"},
        },
    ).json()["question_id"]

    launched = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Paper",
            "question": question,
            "entity_ids": [paper],
            "scope": "cell",
        },
    ).json()
    job = wait_for_job(client, launched["job_id"])
    assert job["outcome"] == "proposals"
    assert [item["action"] for item in job["relationship_suggestions"]] == [
        "link",
        "create_and_link",
    ]
    assert job["relationship_suggestions"][1]["target_name"] == "Katherine Johnson"
    assert job["relationship_suggestions"][1]["proposed_fields"] == {
        "birth_year": 1918,
        "expertise": ["mathematics", "spaceflight"],
    }
    author_rows = client.get("/api/matrix/Author").json()["rows"]
    assert [(row["entity_id"], row["name"]) for row in author_rows] == [(ada, "Ada")]
    suggestion_ids = [item["suggestion_id"] for item in job["relationship_suggestions"]]
    accepted = client.post(
        f"/api/research/jobs/{job['job_id']}/relationships/accept",
        json={"suggestion_ids": suggestion_ids},
    )
    assert accepted.status_code == 201
    assert accepted.json()["count"] == 2
    assert len(accepted.json()["accepted"][1]["populated_claim_ids"]) == 3, accepted.json()
    assert {row["name"] for row in client.get("/api/matrix/Author").json()["rows"]} == {
        "Ada",
        "Katherine Johnson",
    }
    cell = client.get("/api/matrix/Paper").json()["rows"][0]["cells"]["authors"]
    assert {item["name"] for item in cell["references"]} == {"Ada", "Katherine Johnson"}
    author_rows = client.get("/api/matrix/Author").json()["rows"]
    katherine = next(row for row in author_rows if row["name"] == "Katherine Johnson")
    assert katherine["cells"]["birth_year"]["value"] == 1918
    assert set(katherine["cells"]["expertise"]["values"]) == {"mathematics", "spaceflight"}


def test_relationship_review_previews_and_accepts_a_whole_column(tmp_path: Path) -> None:
    def researcher(_kind, _question, entities, progress=None):
        paper = entities[0]
        author = "Ada" if paper["name"] == "Paper One" else "Grace"
        return [
            {
                "entity_id": paper["entity_id"],
                "status": "answered",
                "value": [author],
                "source_type": "web",
                "source_url": f"https://example.test/{author.lower()}",
                "source_title": f"{paper['name']} title page",
                "excerpt": f"{paper['name']} was written by {author}.",
                "confidence": "high",
                "notes": "",
            }
        ]

    database = tmp_path / "column-review.sqlite"
    client = TestClient(create_app(database, tmp_path / "missing", researcher))
    client.post("/api/project", json={"name": "Column review"})
    for name in ["Paper One", "Paper Two"]:
        client.post("/api/entities", json={"kind": "Paper", "name": name})
    question = client.post(
        "/api/questions",
        json={
            "name": "authors",
            "subject_kind": "Paper",
            "value_type": "Ref[Author]",
            "definition": {"cardinality": "many"},
        },
    ).json()["question_id"]
    launched = client.post(
        "/api/research/column",
        json={"entity_kind": "Paper", "question": question, "scope": "column"},
    ).json()
    for job in launched["jobs"]:
        wait_for_job(client, job["job_id"])

    scope = {"scope": "column", "question_id": question, "review_id": "review-column"}
    preview = client.post("/api/research/relationships/preview", json=scope)
    assert preview.status_code == 200
    preview_data = preview.json()
    assert len(preview_data.pop("suggestion_ids")) == 2
    assert preview_data == {
        "review_id": "review-column",
        "scope": "column",
        "count": 2,
        "jobs": 2,
        "subjects": 2,
        "questions": 1,
        "creates": 2,
        "links": 0,
    }
    accepted = client.post("/api/research/relationships/accept", json=scope)
    assert accepted.status_code == 201
    assert accepted.json()["count"] == 2
    rows = client.get("/api/matrix/Paper").json()["rows"]
    assert {row["name"]: row["cells"]["authors"]["references"][0]["name"] for row in rows} == {
        "Paper One": "Ada",
        "Paper Two": "Grace",
    }
    jobs = client.get("/api/research/jobs").json()
    assert all(
        suggestion["status"] == "accepted"
        for job in jobs
        for suggestion in job["relationship_suggestions"]
    )

    paper_three = client.post(
        "/api/entities", json={"kind": "Paper", "name": "Paper Three"}
    ).json()["entity_id"]
    launched = client.post(
        "/api/research/jobs",
        json={
            "entity_kind": "Paper",
            "question": question,
            "entity_ids": [paper_three],
            "scope": "cell",
        },
    ).json()
    wait_for_job(client, launched["job_id"])
    rejected = client.post(
        "/api/research/relationships/reject",
        json={
            "scope": "cell",
            "subject_entity_id": paper_three,
            "question_id": question,
            "review_id": "reject-cell",
            "reason": "Wrong author",
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["count"] == 1
    paper_three_cell = next(
        row
        for row in client.get("/api/matrix/Paper").json()["rows"]
        if row["name"] == "Paper Three"
    )["cells"]["authors"]
    assert paper_three_cell["state"] == "Unasked"
    rejected_job = next(
        job
        for job in client.get("/api/research/jobs").json()
        if job["job_id"] == launched["job_id"]
    )
    assert rejected_job["relationship_suggestions"][0]["status"] == "dismissed"
    assert Store(database).history()[-1]["event_type"] == "relationship.review_rejected"


def test_relationship_cardinality_mismatches_are_bulk_approved(tmp_path: Path) -> None:
    def researcher(_kind, _question, entities, progress=None):
        name = entities[0]["name"]
        authors = ["Ada", "Grace"] if name == "Paper One" else ["Grace", "Katherine"]
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "answered",
                "value": authors,
                "source_type": "web",
                "source_url": f"https://example.test/{name.lower().replace(' ', '-')}",
                "source_title": f"{name} title page",
                "excerpt": f"{name} was written by {', '.join(authors)}.",
                "confidence": "high",
                "notes": "",
            }
        ]

    client = TestClient(
        create_app(tmp_path / "bulk-relationship.sqlite", tmp_path / "missing", researcher)
    )
    client.post("/api/project", json={"name": "Bulk relationship adaptation"})
    for name in ["Paper One", "Paper Two"]:
        client.post("/api/entities", json={"kind": "Paper", "name": name})
    question = client.post(
        "/api/questions",
        json={
            "name": "authors",
            "subject_kind": "Paper",
            "value_type": "Ref[Author]",
            "definition": {"label": "Authors", "cardinality": "one"},
        },
    ).json()["question_id"]

    launched = client.post(
        "/api/research/column",
        json={"entity_kind": "Paper", "question": question, "scope": "column"},
    ).json()
    jobs = [wait_for_job(client, item["job_id"]) for item in launched["jobs"]]
    assert {job["outcome"] for job in jobs} == {"schema_proposal"}
    assert all(job["schema_adaptation"]["status"] == "pending" for job in jobs)

    accepted = client.post(f"/api/research/schema-adaptations/{question}/accept", json={})
    assert accepted.status_code == 201
    result = accepted.json()
    assert result["accepted_relationships"] == 4
    matrix = client.get("/api/matrix/Paper").json()
    assert matrix["questions"][0]["definition"]["cardinality"] == "many"
    assert [
        {item["name"] for item in row["cells"]["authors"]["references"]} for row in matrix["rows"]
    ] == [{"Ada", "Grace"}, {"Grace", "Katherine"}]
    refreshed = client.get("/api/research/jobs").json()
    assert all(job["schema_adaptation"]["status"] == "applied" for job in refreshed)


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
    if importlib.util.find_spec("edsl") is not None:
        scenarios = client.get("/api/export/Company.scenario-list.ep")
        assert scenarios.status_code == 200
        assert scenarios.content.startswith(b"PK")
        assert "Company.scenario-list.ep" in scenarios.headers["content-disposition"]
        agents = client.get("/api/export/Company.agent-list.ep")
        assert agents.status_code == 200
        assert agents.content.startswith(b"PK")
        assert "Company.agent-list.ep" in agents.headers["content-disposition"]
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


def test_project_sqlite_import_creates_and_opens_managed_copy(tmp_path: Path) -> None:
    source = tmp_path / "paul-graham.sqlite"
    source_store = Store(source)
    source_store.initialize("Paul Graham and his writing")
    source_store.add_entity("Person", "Paul Graham", {}, "test")
    portable = tmp_path / "paul-graham-portable.sqlite"
    source_store.backup(portable)
    original_bytes = portable.read_bytes()

    projects = tmp_path / "projects"
    client = TestClient(
        create_app(tmp_path / "current.sqlite", tmp_path / "missing", projects_directory=projects)
    )
    response = client.post(
        "/api/projects/import?filename=paul-graham.sqlite",
        content=original_bytes,
        headers={"Content-Type": "application/vnd.sqlite3"},
    )

    assert response.status_code == 201
    imported = response.json()
    assert imported["name"] == "Paul Graham and his writing"
    assert imported["active"] is True
    assert Path(imported["path"]).parent == projects
    assert Path(imported["path"]) != portable
    assert portable.read_bytes() == original_bytes
    assert client.get("/api/project").json()["entity_kinds"] == [
        {"kind": "Person", "entities": 1, "questions": 0}
    ]
    downloaded = client.get("/api/export/project.sqlite")
    assert downloaded.status_code == 200
    restored = tmp_path / "downloaded.sqlite"
    restored.write_bytes(downloaded.content)
    assert Store(restored).overview()["project"]["name"] == "Paul Graham and his writing"


def test_project_sqlite_import_rejects_invalid_and_non_epiq_files(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            tmp_path / "current.sqlite",
            tmp_path / "missing",
            projects_directory=tmp_path / "projects",
        )
    )
    invalid = client.post(
        "/api/projects/import?filename=notes.sqlite",
        content=b"this is not sqlite",
        headers={"Content-Type": "application/vnd.sqlite3"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_project_file"

    ordinary = tmp_path / "ordinary.sqlite"
    connection = sqlite3.connect(ordinary)
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.commit()
    connection.close()
    not_epiq = client.post(
        "/api/projects/import?filename=ordinary.sqlite",
        content=ordinary.read_bytes(),
        headers={"Content-Type": "application/vnd.sqlite3"},
    )
    assert not_epiq.status_code == 400
    assert not_epiq.json()["error"]["code"] == "not_epiq_project"
    assert list((tmp_path / "projects").glob("*.sqlite")) == []


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


def test_workspace_agent_applies_schema_rows_and_launches_cell_research(
    tmp_path: Path,
) -> None:
    captured = {}

    def planner(goal, context, progress=None):
        captured.update({"goal": goal, "context": context})
        if progress:
            progress("Designed an AI interviewer market workspace")
        return {
            "summary": "Created a startup market map with company facts and founders.",
            "entity_kinds": ["Startup", "Founder"],
            "entities": [
                {"kind": "Startup", "name": "Listen Labs"},
                {"kind": "Startup", "name": "Outset"},
            ],
            "questions": [
                {
                    "kind": "Startup",
                    "name": "website",
                    "value_type": "URL",
                    "label": "Website",
                    "cardinality": "one",
                    "volatility": "slow",
                    "freshness_days": 180,
                    "research_guidance": "Use the company's official website.",
                },
                {
                    "kind": "Startup",
                    "name": "founders",
                    "value_type": "Ref[Founder]",
                    "label": "Founders",
                    "cardinality": "many",
                    "volatility": "stable",
                    "freshness_days": None,
                    "research_guidance": "Find named company founders.",
                },
            ],
            "research": [
                {
                    "kind": "Startup",
                    "question": "website",
                    "entity_names": ["Listen Labs", "Outset"],
                    "instructions": "Verify the official company website.",
                }
            ],
        }

    def researcher(_kind, _question, entities, progress=None):
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "not_found",
                "notes": "Test runner intentionally did not search.",
            }
        ]

    client = TestClient(
        create_app(
            tmp_path / "workspace-agent.sqlite",
            tmp_path / "missing",
            researcher,
            workspace_agent_runner=planner,
        )
    )
    client.post("/api/project", json={"name": "Market research"})
    launched = client.post(
        "/api/workspace-agent/jobs",
        json={"message": "Collect data on AI interviewer startups"},
    )
    assert launched.status_code == 202
    proposal = wait_for_job(client, launched.json()["job_id"])
    assert proposal["status"] == "completed"
    assert proposal["outcome"] == "workspace_proposal"
    assert proposal["approval_status"] == "pending"
    assert proposal["assistant_summary"].startswith("Created a startup")
    assert client.get("/api/project").json()["entity_kinds"] == []
    approved = client.post(f"/api/workspace-agent/jobs/{proposal['job_id']}/approve")
    assert approved.status_code == 202
    parent = wait_for_job(client, proposal["job_id"])
    assert len(parent["child_job_ids"]) == 2
    for child_job_id in parent["child_job_ids"]:
        assert wait_for_job(client, child_job_id)["status"] == "completed"

    matrix = client.get("/api/matrix/Startup").json()
    assert [row["name"] for row in matrix["rows"]] == ["Listen Labs", "Outset"]
    assert {question["name"] for question in matrix["questions"]} == {
        "website",
        "founders",
    }
    assert captured["goal"] == "Collect data on AI interviewer startups"
    assert captured["context"]["project"]["name"] == "Market research"
    available = {item["operation"] for item in captured["context"]["available_operations"]}
    assert {"entity", "question", "record", "derive.distribution"} <= available
    assert "migrate" not in available


def test_workspace_agent_repairs_misplaced_research_table_and_field_names(
    tmp_path: Path,
) -> None:
    researched = []

    def planner(_goal, _context, progress=None):
        return {
            "summary": "Verify the seeded action row.",
            "entity_kinds": [],
            "entities": [],
            "questions": [],
            "research": [
                {
                    "kind": "primary_source_verification",
                    "question": "Find an official source and verify action_date and title.",
                    "entity_names": ["Prediction Markets Act introduction"],
                    "instructions": "Target existing Actions questions: action_date, title.",
                }
            ],
        }

    def researcher(_kind, question, entities, progress=None):
        researched.append((question["name"], entities[0]["name"]))
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "not_found",
                "notes": "Test runner did not search.",
            }
        ]

    client = TestClient(
        create_app(
            tmp_path / "repair-workspace.sqlite",
            tmp_path / "missing",
            researcher,
            workspace_agent_runner=planner,
        )
    )
    client.post("/api/project", json={"name": "Prediction markets"})
    client.post(
        "/api/entities",
        json={"kind": "Actions", "name": "Prediction Markets Act introduction"},
    )
    for name, value_type in (("action_date", "Date"), ("title", "String")):
        client.post(
            "/api/questions",
            json={"name": name, "subject_kind": "Actions", "value_type": value_type},
        )

    launched = client.post(
        "/api/workspace-agent/jobs", json={"message": "Verify the existing action"}
    ).json()
    proposal = wait_for_job(client, launched["job_id"])
    assert proposal["approval_status"] == "pending"
    assert researched == []
    approved = client.post(f"/api/workspace-agent/jobs/{proposal['job_id']}/approve")
    assert approved.status_code == 202
    parent = wait_for_job(client, launched["job_id"])
    assert len(parent["child_job_ids"]) == 2
    for child_job_id in parent["child_job_ids"]:
        assert wait_for_job(client, child_job_id)["status"] == "completed"
    assert set(researched) == {
        ("action_date", "Prediction Markets Act introduction"),
        ("title", "Prediction Markets Act introduction"),
    }
    assert any("Resolved research table" in message["message"] for message in parent["messages"])


def test_research_splits_many_valued_results_into_typed_scalar_claims(
    tmp_path: Path,
) -> None:
    def researcher(_kind, question, entities, progress=None):
        values = (
            ["Healthcare", "SaaS"]
            if question["name"] == "verticals"
            else ["roleplay", "call_analysis"]
        )
        return [
            {
                "entity_id": entities[0]["entity_id"],
                "status": "answered",
                "value": values,
                "source_type": "web",
                "source_url": f"https://example.test/{question['name']}",
                "source_title": "Product overview",
                "excerpt": "The product overview supports each listed value.",
                "confidence": "high",
                "notes": "",
            }
        ]

    client = TestClient(
        create_app(tmp_path / "many-values.sqlite", tmp_path / "missing", researcher)
    )
    client.post("/api/project", json={"name": "Many-valued research"})
    entity_id = client.post("/api/entities", json={"kind": "Startup", "name": "Acme"}).json()[
        "entity_id"
    ]
    questions = {}
    for name, value_type in (
        ("verticals", "String"),
        ("workflows", "Enum[roleplay,call_analysis,deal_coaching]"),
    ):
        questions[name] = client.post(
            "/api/questions",
            json={
                "name": name,
                "subject_kind": "Startup",
                "value_type": value_type,
                "definition": {"cardinality": "many"},
            },
        ).json()["question_id"]
        launched = client.post(
            "/api/research/jobs",
            json={
                "entity_kind": "Startup",
                "question": questions[name],
                "entity_ids": [entity_id],
                "scope": "cell",
            },
        ).json()
        completed = wait_for_job(client, launched["job_id"])
        assert completed["status"] == "completed"
        assert completed["written"] == 1

    cells = client.get("/api/matrix/Startup").json()["rows"][0]["cells"]
    assert set(cells["verticals"]["values"]) == {"Healthcare", "SaaS"}
    assert set(cells["workflows"]["values"]) == {"roleplay", "call_analysis"}
    assert len(cells["verticals"]["lineage"]) == 2


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
