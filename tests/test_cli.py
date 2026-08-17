import json
from pathlib import Path

import pytest

from epiq.cli import main
from epiq.store import Store


def test_cli_demo_round_trip(tmp_path: Path, capsys) -> None:
    database = tmp_path / "demo.sqlite"
    main(["--db", str(database), "init", "--name", "Demo"])
    assert json.loads(capsys.readouterr().out)["ok"] is True

    main(["--db", str(database), "demo", "patriots"])
    demo = json.loads(capsys.readouterr().out)
    assert demo["final"]["record"] == "14-3"

    main(["--db", str(database), "season-record", "New England Patriots 2025"])
    record = json.loads(capsys.readouterr().out)
    assert record["wins"] == 14
    assert len(record["lineage"]) == 17

    report = tmp_path / "report.html"
    main(["--db", str(database), "export-html", "--kind", "Game", "--output", str(report)])
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert "Patriots 2025 Week 1" in report.read_text()


def test_cli_can_remember_workspace_database(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "research" / "market.sqlite"

    main(["use", str(database)])
    selected = json.loads(capsys.readouterr().out)
    assert selected["exists"] is False

    main(["db"])
    current = json.loads(capsys.readouterr().out)
    assert current["database"] == str(database)
    assert current["source"] == "workspace"

    main(["init", "--name", "Market"])
    assert json.loads(capsys.readouterr().out)["database"] == str(database)
    assert database.exists()

    environment_database = tmp_path / "environment.sqlite"
    monkeypatch.setenv("EPIQ_DB", str(environment_database))
    main(["db"])
    current = json.loads(capsys.readouterr().out)
    assert current["database"] == str(environment_database)
    assert current["source"] == "environment"


def test_cli_derives_distribution_from_repeated_input_claims(tmp_path: Path, capsys) -> None:
    database = tmp_path / "weather.sqlite"
    store = Store(database)
    store.initialize("Weather")
    event = store.add_entity("WeatherEvent", "Tomorrow", {}, "test")
    store.add_question(
        "probability", "WeatherEvent", "Probability", {"cardinality": "many"}, "test"
    )
    store.add_question("ensemble", "WeatherEvent", "Distribution[Float]", {}, "test")
    inputs = []
    for index, value in enumerate([0.4, 0.6]):
        _, evidence = store.add_evidence(
            f"https://example.test/{index}", "Forecast", "2026-08-16", str(value), "test"
        )
        inputs.append(
            store.assert_claim(event, "probability", value, "2026-08-17", evidence, "test")
        )

    main(
        [
            "--db",
            str(database),
            "derive-distribution",
            "--subject",
            event,
            "--question",
            "ensemble",
            "--input-claim",
            inputs[0],
            "--input-claim",
            inputs[1],
            "--valid-from",
            "2026-08-17",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["input_claim_ids"] == inputs
    assert store.matrix("WeatherEvent")["rows"][0]["cells"]["ensemble"]["value"] == {
        "kind": "empirical",
        "samples": [0.4, 0.6],
    }


def test_cli_crud_matrix_history_and_retraction(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Market")
    entity = invoke("entity", "Company", "Example", "--attributes", '{"domain":"example.test"}')
    question = invoke(
        "question",
        "status",
        "--for",
        "Company",
        "--type",
        "String",
        "--definition",
        '{"label":"Status"}',
    )
    evidence = invoke(
        "evidence",
        "--url",
        "https://example.test/about",
        "--title",
        "About",
        "--retrieved-at",
        "2026-08-16",
        "--excerpt",
        "The company is active.",
    )
    claim = invoke(
        "assert",
        "--subject",
        entity["entity_id"],
        "--question",
        question["question_id"],
        "--value",
        "active",
        "--valid-from",
        "2026-08-16",
        "--evidence",
        evidence["evidence_id"],
        "--confidence",
        "medium",
    )

    matrix = invoke("matrix", "--kind", "Company", "--questions", "status")
    assert matrix["rows"][0]["cells"]["status"]["value"] == "active"
    assert len(invoke("history", "--type", "claim.assert")) == 1
    assert invoke("retract", claim["claim_id"], "--reason", "Outdated")["status"] == "retracted"
    assert invoke("matrix", "--kind", "Company")["rows"][0]["cells"]["status"]["state"] == "Unasked"


def test_cli_claim_review_and_atomic_bulk_write(tmp_path: Path, capsys) -> None:
    database = tmp_path / "review.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Review")
    entity = invoke("entity", "Company", "Acme")["entity_id"]
    invoke("question", "active", "--for", "Company", "--type", "Bool")
    evidence = invoke(
        "evidence",
        "--url",
        "https://example.test/acme",
        "--title",
        "Acme",
        "--retrieved-at",
        "2026-08-17",
        "--excerpt",
        "Acme is active.",
    )["evidence_id"]
    proposal = invoke(
        "propose-claim",
        "--subject",
        entity,
        "--question",
        "active",
        "--value",
        "true",
        "--valid-from",
        "2026-08-17",
        "--evidence",
        evidence,
    )["proposal_id"]
    assert invoke("claim-proposals")["proposals"][0]["proposal_id"] == proposal
    invoke("review-claims", proposal, "--decision", "approved", "--reason", "Verified")
    assert invoke("matrix", "--kind", "Company")["rows"][0]["cells"]["active"]["value"] is True

    batch = tmp_path / "claims.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "subject": entity,
                    "question": "active",
                    "value": False,
                    "valid_from": "2026-08-18",
                    "evidence_ids": [evidence],
                }
            ]
        )
    )
    assert invoke("bulk-assert", "--input", str(batch))["count"] == 1


def test_cli_apply_concise_query_output_and_non_web_evidence(tmp_path: Path, capsys) -> None:
    database = tmp_path / "ergonomics.sqlite"
    declaration = tmp_path / "project.json"
    declaration.write_text(
        json.dumps(
            {
                "project": {"name": "Ergonomics"},
                "entities": [{"kind": "Person", "name": "Ada"}],
                "questions": [{"name": "born", "subject_kind": "Person", "value_type": "Year"}],
            }
        )
    )
    main(["--db", str(database), "--quiet", "apply", "--input", str(declaration)])
    assert capsys.readouterr().out == ""

    notes = tmp_path / "notes.md"
    notes.write_text("Ada was born in 1815.")
    main(
        [
            "--db",
            str(database),
            "evidence",
            "--type",
            "personal",
            "--title",
            "Research notes",
            "--retrieved-at",
            "2026-08-17",
            "--excerpt-file",
            str(notes),
        ]
    )
    evidence = json.loads(capsys.readouterr().out)["evidence_id"]
    main(
        [
            "--db",
            str(database),
            "assert",
            "--subject",
            "Ada",
            "--question",
            "born",
            "--value",
            "1815",
            "--valid-from",
            "1815-12-10",
            "--evidence",
            evidence,
        ]
    )
    capsys.readouterr()
    main(
        [
            "--db",
            str(database),
            "--select",
            "query.matched",
            "query",
            "--kind",
            "Person",
            "--where",
            "born >= 1800",
        ]
    )
    assert json.loads(capsys.readouterr().out) == 1


def test_cli_question_challenge_lifecycle(tmp_path: Path, capsys) -> None:
    database = tmp_path / "boats.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Boats")
    boat = invoke("entity", "BoatModel", "RS Quest")
    question = invoke("question", "has_spinnaker", "--for", "BoatModel", "--type", "Bool")
    challenge = invoke(
        "challenge-question",
        question["question_id"],
        "--problem",
        "modal_ambiguity",
        "--explanation",
        "Can have is different from currently has.",
        "--example-entity",
        boat["entity_id"],
        "--proposed-replacement",
        '{"name":"spinnaker_availability","value_type":"Enum[standard,optional,unavailable]"}',
    )
    listed = invoke("question-challenges", "--status", "open")
    assert listed[0]["challenge_id"] == challenge["challenge_id"]
    resolved = invoke(
        "resolve-question-challenge",
        challenge["challenge_id"],
        "--status",
        "dismissed",
        "--resolution",
        "Keep the Boolean but clarify its definition.",
    )
    assert resolved["status"] == "dismissed"


def test_cli_records_not_found_and_exports_xlsx(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.sqlite"
    store = Store(database)
    store.initialize("Market")
    company = store.add_entity("Company", "Example", {}, "test")
    store.add_question("pricing", "Company", "String", {}, "test")

    main(
        [
            "--db",
            str(database),
            "not-found",
            "--subject",
            company,
            "--question",
            "pricing",
            "--query",
            "site:example.test pricing",
            "--notes",
            "No public price found.",
        ]
    )
    assert json.loads(capsys.readouterr().out)["state"] == "NotFound"

    output = tmp_path / "market.xlsx"
    main(["--db", str(database), "export-xlsx", "--kind", "Company", "--output", str(output)])
    result = json.loads(capsys.readouterr().out)
    assert result["entities"] == 1
    assert result["questions"] == 1
    assert output.read_bytes().startswith(b"PK")


def test_cli_errors_are_single_machine_readable_json_values(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(SystemExit) as exit_info:
        main(["--db", str(missing), "matrix", "--kind", "Company"])
    assert exit_info.value.code == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "project_not_found"
    assert "Run:" in error["suggestion"]

    database = tmp_path / "market.sqlite"
    main(["--db", str(database), "init", "--name", "Market"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        main(["--db", str(database), "entity", "Company", "Example", "--attributes", "[]"])
    assert exit_info.value.code == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "invalid_attributes"

    with pytest.raises(SystemExit) as exit_info:
        main(["--db", str(database), "entity", "Company", "Example", "--attributes", "{"])
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"


def test_cli_rejects_non_array_distribution_weights(tmp_path: Path, capsys) -> None:
    database = tmp_path / "weather.sqlite"
    Store(database).initialize("Weather")
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--db",
                str(database),
                "derive-distribution",
                "--subject",
                "missing",
                "--question",
                "missing",
                "--input-claim",
                "clm_missing",
                "--weights",
                '{"first":1}',
                "--valid-from",
                "2026-08-17",
            ]
        )
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_weights"


def test_cli_operational_and_agent_orientation_commands(tmp_path: Path, capsys) -> None:
    database = tmp_path / "market.sqlite"
    store = Store(database)
    store.initialize("Market")
    store.add_entity("Company", "Example", {}, "test")
    store.add_question("employee_count", "Company", "Int", {"label": "Employees"}, "test")

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    assert invoke("doctor")["ok"] is True
    schema = invoke("schema", "--kind", "Company")
    assert schema["tables"][0]["questions"][0]["name"] == "employee_count"
    assert "Probability" in schema["value_types"]
    context = invoke("context", "--kind", "Company", "--budget", "100")
    assert context["project"]["name"] == "Market"
    gaps = invoke("gaps", "--kind", "Company")
    assert gaps["cells"][0]["state"] == "Unasked"
    plan = invoke("refresh-plan", "--kind", "Company")
    assert plan["tasks"][0]["reasons"] == ["gap"]
    assert plan["tasks"][0]["suggested_query"] == '"Example" Employees'
    assert invoke("contradictions", "--kind", "Company")["count"] == 0
    search = invoke("search", "Example")
    assert any(item["record_type"] == "entity" for item in search["results"])

    backup = tmp_path / "backups" / "market.sqlite"
    result = invoke("backup", "--output", str(backup))
    assert result["ok"] is True
    assert Store(backup).doctor()["ok"] is True
