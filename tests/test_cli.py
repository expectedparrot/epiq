import json
import sqlite3
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


def test_capabilities_are_machine_readable_without_a_project(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.sqlite"
    main(["--db", str(missing), "capabilities", "--command", "record"])
    result = json.loads(capsys.readouterr().out)

    assert result["protocol"] == {
        "epiq_version": "0.1.0",
        "name": "epiq-cli",
        "version": 1,
    }
    command = result["commands"][0]
    assert command["name"] == "record"
    assert command["mutates_project"] is True
    assert command["transactional"] is True
    assert command["example"].startswith("epiq --actor")
    assert {item["name"] for item in command["arguments"]} >= {
        "subject",
        "source_type",
        "excerpt",
        "cell",
    }
    assert command["constraints"] == [
        {"members": ["excerpt", "excerpt_file"], "required": True, "rule": "exactly_one"}
    ]
    assert result["document_schemas"]["apply"]["questions"]


def test_schema_and_context_include_question_only_kind(tmp_path: Path, capsys) -> None:
    database = tmp_path / "empty-kind.sqlite"
    main(["--db", str(database), "init", "--name", "Question first"])
    capsys.readouterr()
    main(["--db", str(database), "question", "score", "--for", "Item", "--type", "Float"])
    capsys.readouterr()
    # Simulate a pre-fix database where defining a question did not register its empty kind.
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM entity_kinds WHERE kind='Item'")

    main(["--db", str(database), "schema"])
    schema = json.loads(capsys.readouterr().out)
    assert schema["tables"][0]["entity_kind"] == "Item"
    assert schema["tables"][0]["questions"][0]["name"] == "score"

    main(["--db", str(database), "context", "--budget", "1000"])
    context = json.loads(capsys.readouterr().out)
    assert context["tables"][0]["entity_kind"] == "Item"
    assert context["tables"][0]["rows"] == []

    main(["--db", str(database), "capabilities", "--include-schema"])
    capabilities = json.loads(capsys.readouterr().out)
    assert capabilities["project_schema"]["tables"][0]["entity_kind"] == "Item"


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


def test_cli_derive_uses_claim_backed_weights(tmp_path: Path, capsys) -> None:
    database = tmp_path / "weighted.sqlite"
    store = Store(database)
    store.initialize("Weighted")
    for kind, name in [("Review", "Review"), ("Finding", "A"), ("Finding", "B")]:
        store.add_entity(kind, name, {}, "test")
    store.add_question("effect", "Finding", "Float", {}, "test")
    store.add_question("sample_size", "Finding", "Int", {}, "test")
    store.add_question("pooled", "Review", "Float", {}, "test")
    for name, effect, size in [("A", 0.2, 100), ("B", 0.5, 200)]:
        _, evidence = store.add_evidence(
            f"https://example.test/{name}", name, "2026-08-17", "Finding", "test"
        )
        store.assert_claim(name, "effect", effect, "2026-08-17", evidence, "test")
        store.assert_claim(name, "sample_size", size, "2026-08-17", evidence, "test")

    main(
        [
            "--db",
            str(database),
            "derive",
            "--subject",
            "Review",
            "--question",
            "pooled",
            "--operation",
            "weighted_avg",
            "--input-cell",
            "A",
            "effect",
            "--input-cell",
            "B",
            "effect",
            "--weight-cell",
            "A",
            "sample_size",
            "--weight-cell",
            "B",
            "sample_size",
            "--valid-from",
            "2026-08-17",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert len(result["parameter_claim_ids"]) == 2
    cell = store.matrix("Review")["rows"][0]["cells"]["pooled"]
    assert cell["value"] == pytest.approx(0.4)
    assert cell["lineage"][0]["derivation"]["parameters"]["parameter_claim_ids"]


def test_cli_materializes_declarative_formulas_per_row(tmp_path: Path, capsys) -> None:
    database = tmp_path / "formula.sqlite"
    store = Store(database)
    store.initialize("Formula")
    for name in ("Atlas", "Beacon"):
        store.add_entity("Quote", name, {}, "test")
    store.add_question("price", "Quote", "Float", {}, "test")
    store.add_question("shipping", "Quote", "Float", {}, "test")
    store.add_question(
        "landed",
        "Quote",
        "Float",
        {"formula": {"operation": "sum", "inputs": ["price", "shipping"]}},
        "test",
    )
    _, evidence = store.add_evidence("urn:test:quotes", "Quotes", "2026-08-17", "Quotes", "test")
    for name, price, shipping in (("Atlas", 40.0, 5.0), ("Beacon", 42.0, 1.0)):
        store.assert_claim(name, "price", price, "2026-08-17", evidence, "test")
        store.assert_claim(name, "shipping", shipping, "2026-08-17", evidence, "test")

    main(["--db", str(database), "materialize", "--kind", "Quote", "--valid-from", "2026-08-17"])
    result = json.loads(capsys.readouterr().out)
    assert [item["status"] for item in result["results"]] == ["materialized", "materialized"]
    assert [row["cells"]["landed"]["value"] for row in store.matrix("Quote")["rows"]] == [
        45.0,
        43.0,
    ]


def test_cli_propagates_claim_through_relationship_path(tmp_path: Path, capsys) -> None:
    database = tmp_path / "propagate.sqlite"
    store = Store(database)
    store.initialize("Propagation")
    for name in ("Acorn", "Beacon", "Cobalt"):
        store.add_entity("Company", name, {}, "test")
    store.add_question("parent", "Company", "Ref[Company]", {}, "test")
    store.add_question("risk", "Company", "String", {}, "test")
    store.add_question("inherited_risk", "Company", "String", {}, "test")
    _, evidence = store.add_evidence(
        "urn:test:ownership", "Registry", "2026-08-17", "Ownership and risk", "test"
    )
    store.assert_claim("Acorn", "parent", "Beacon", "2026-08-17", evidence, "test")
    store.assert_claim("Beacon", "parent", "Cobalt", "2026-08-17", evidence, "test")
    store.assert_claim("Cobalt", "risk", "high", "2026-08-17", evidence, "test")

    main(
        [
            "--db",
            str(database),
            "propagate",
            "--subject",
            "Acorn",
            "--via",
            "parent",
            "--question",
            "risk",
            "--to-question",
            "inherited_risk",
            "--depth",
            "5",
            "--valid-from",
            "2026-08-17",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert result["source_entity"] == "Cobalt"
    cell = next(row for row in store.matrix("Company")["rows"] if row["name"] == "Acorn")["cells"][
        "inherited_risk"
    ]
    assert cell["value"] == "high"
    assert cell["lineage"][0]["derivation"]["operation"] == "copy"
    dependencies = cell["lineage"][0]["derivation"]["dependencies"]
    assert [item["role"] for item in dependencies].count("path") == 2

    _, update_evidence = store.add_evidence(
        "urn:test:risk-update", "Risk update", "2026-08-18", "Risk is medium", "test"
    )
    store.assert_claim("Cobalt", "risk", "medium", "2026-08-18", update_evidence, "test")
    main(["--db", str(database), "stale-derivations"])
    stale = json.loads(capsys.readouterr().out)
    assert stale["count"] == 1
    assert stale["stale_derivations"][0]["reasons"] == [
        {
            "dependency_claim_id": dependencies[0]["claim_id"],
            "reason": "newer_claim_available",
            "role": "operand",
        }
    ]

    store.add_entity("Company", "Delta", {}, "test")
    store.assert_claim("Beacon", "parent", "Delta", "2026-08-18", evidence, "test")
    main(["--db", str(database), "stale-derivations"])
    stale = json.loads(capsys.readouterr().out)
    assert any(
        reason["role"] == "path" and reason["reason"] == "newer_claim_available"
        for reason in stale["stale_derivations"][0]["reasons"]
    )


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


def test_cli_record_atomically_adds_evidence_and_supported_answers(tmp_path: Path, capsys) -> None:
    database = tmp_path / "record.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Record")
    invoke("entity", "Candidate", "Alex Rivera")
    invoke("entity", "Role", "Product Engineer")
    invoke("question", "rating", "--for", "Candidate", "--type", "Probability")
    invoke("question", "role", "--for", "Candidate", "--type", "Ref[Role]")
    invoke(
        "question",
        "decision",
        "--for",
        "Candidate",
        "--type",
        "Enum[hire,hold]",
    )

    result = invoke(
        "record",
        "--subject",
        "Alex Rivera",
        "--source-type",
        "interview",
        "--source-title",
        "Technical interview",
        "--retrieved-at",
        "2026-08-17",
        "--excerpt",
        "Confidence 0.82; recommended for Product Engineer.",
        "--valid-from",
        "2026-08-17",
        "--answer",
        "rating",
        "0.82",
        "--answer",
        "role",
        "Product Engineer",
    )
    assert result["answer_count"] == 2
    assert len(result["claim_ids"]) == 2
    matrix = invoke("matrix", "--kind", "Candidate")
    assert matrix["rows"][0]["cells"]["rating"]["value"] == 0.82
    assert matrix["rows"][0]["cells"]["role"]["display_value"]["name"] == "Product Engineer"
    assert {
        item["evidence_id"]
        for question in ("rating", "role")
        for item in matrix["rows"][0]["cells"][question]["lineage"]
    } == {result["evidence_id"]}

    single = invoke(
        "record",
        "--subject",
        "Alex Rivera",
        "--type",
        "report",
        "--title",
        "Committee decision",
        "--retrieved-at",
        "2026-08-18",
        "--excerpt",
        "The committee decision is hire.",
        "--valid-from",
        "2026-08-18",
        "--question",
        "decision",
        "--value",
        "hire",
    )
    assert single["answer_count"] == 1
    assert (
        invoke("matrix", "--kind", "Candidate")["rows"][0]["cells"]["decision"]["value"] == "hire"
    )


def test_cli_record_rolls_back_evidence_when_an_answer_is_invalid(tmp_path: Path, capsys) -> None:
    database = tmp_path / "record-rollback.sqlite"
    store = Store(database)
    store.initialize("Record rollback")
    store.add_entity("Candidate", "Alex Rivera", {}, "test")
    store.add_question("rating", "Candidate", "Probability", {}, "test")
    before = store.doctor()["counts"]

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "--db",
                str(database),
                "record",
                "--subject",
                "Alex Rivera",
                "--type",
                "interview",
                "--title",
                "Interview",
                "--retrieved-at",
                "2026-08-17",
                "--excerpt",
                "Invalid confidence value.",
                "--valid-from",
                "2026-08-17",
                "--answer",
                "rating",
                "1.5",
            ]
        )
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "value_type_error"
    after = store.doctor()["counts"]
    assert after["evidence"] == before["evidence"]
    assert after["events"] == before["events"]


def test_cli_record_can_write_multiple_subjects_and_render_table(tmp_path: Path, capsys) -> None:
    database = tmp_path / "multi-record.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Multi record")
    invoke("entity", "Company", "Acorn")
    invoke("entity", "Company", "Beacon")
    invoke("question", "price", "--for", "Company", "--type", "Int")
    result = invoke(
        "record",
        "--source-type",
        "report",
        "--source-title",
        "Price report",
        "--retrieved-at",
        "2026-08-17",
        "--excerpt",
        "Acorn costs 10 and Beacon costs 20.",
        "--valid-from",
        "2026-08-17",
        "--cell",
        "Acorn",
        "price",
        "10",
        "--cell",
        "Beacon",
        "price",
        "20",
    )
    assert result["answer_count"] == 2
    main(["--db", str(database), "--format", "table", "matrix", "--kind", "Company"])
    output = capsys.readouterr().out
    assert "Company" in output and "price" in output
    assert "Acorn" in output and "10" in output
    assert "Beacon" in output and "20" in output


def test_cli_related_can_traverse_multiple_hops(tmp_path: Path, capsys) -> None:
    database = tmp_path / "graph.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Graph")
    for name in ("A", "B", "C"):
        invoke("entity", "Node", name)
    invoke(
        "question",
        "next_node",
        "--for",
        "Node",
        "--type",
        "Ref[Node]",
        "--definition",
        '{"cardinality":"many"}',
    )
    for source, target in (("A", "B"), ("B", "C")):
        invoke(
            "record",
            "--subject",
            source,
            "--source-type",
            "report",
            "--source-title",
            f"{source} to {target}",
            "--retrieved-at",
            "2026-08-17",
            "--excerpt",
            f"{source} points to {target}.",
            "--valid-from",
            "2026-08-17",
            "--question",
            "next_node",
            "--value",
            target,
        )
    result = invoke("related", "A", "--direction", "outgoing", "--depth", "2")
    assert result["count"] == 2
    assert [edge["to"]["name"] for edge in result["edges"]] == ["B", "C"]
    assert [edge["depth"] for edge in result["edges"]] == [1, 2]


def test_cli_aggregate_groups_numeric_claims(tmp_path: Path, capsys) -> None:
    database = tmp_path / "aggregate.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Aggregate")
    for name in ("A", "B", "C"):
        invoke("entity", "Quote", name)
    invoke("question", "region", "--for", "Quote", "--type", "String")
    invoke("question", "price", "--for", "Quote", "--type", "Float")
    invoke(
        "record",
        "--source-type",
        "report",
        "--source-title",
        "Quotes",
        "--retrieved-at",
        "2026-08-17",
        "--excerpt",
        "Three regional quotes.",
        "--valid-from",
        "2026-08-17",
        "--cell",
        "A",
        "region",
        "US",
        "--cell",
        "A",
        "price",
        "10",
        "--cell",
        "B",
        "region",
        "US",
        "--cell",
        "B",
        "price",
        "20",
        "--cell",
        "C",
        "region",
        "EU",
        "--cell",
        "C",
        "price",
        "40",
    )
    result = invoke(
        "aggregate",
        "--kind",
        "Quote",
        "--question",
        "price",
        "--op",
        "avg",
        "--group-by",
        "region",
    )
    assert result["groups"] == [
        {"group": "EU", "value": 40.0, "count": 1},
        {"group": "US", "value": 15.0, "count": 2},
    ]


def test_cli_structured_locator_and_source_entity_appear_in_lineage(tmp_path: Path, capsys) -> None:
    database = tmp_path / "locator.sqlite"

    def invoke(*arguments: str):
        main(["--db", str(database), *arguments])
        return json.loads(capsys.readouterr().out)

    invoke("init", "--name", "Locator")
    invoke("entity", "Paper", "Study A")
    invoke("entity", "Finding", "Finding A")
    invoke("question", "effect", "--for", "Finding", "--type", "Float")
    invoke(
        "record",
        "--subject",
        "Finding A",
        "--source-type",
        "report",
        "--source-title",
        "Study A",
        "--source-entity",
        "Study A",
        "--locator",
        '{"page":12,"table":"3"}',
        "--retrieved-at",
        "2026-08-17",
        "--excerpt",
        "Effect 0.18.",
        "--valid-from",
        "2026-08-17",
        "--question",
        "effect",
        "--value",
        "0.18",
    )
    source = invoke("matrix", "--kind", "Finding")["rows"][0]["cells"]["effect"]["lineage"][0][
        "source"
    ]
    assert source["locator"] == {"page": 12, "table": "3"}
    assert source["linked_entity_id"]


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
