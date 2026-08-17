import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from epiq.demo import load_patriots
from epiq.errors import EpiqError
from epiq.store import Store, _evaluate_arithmetic_expression, canonicalize_url


@pytest.fixture
def store(tmp_path: Path) -> Store:
    result = Store(tmp_path / "epiq.sqlite")
    result.initialize("test")
    return result


def test_patriots_record_changes_over_transaction_time(store: Store) -> None:
    loaded = load_patriots(store)
    season = str(loaded["season_id"])

    assert store.season_record(season, "2025-09-06T23:59:59Z")["record"] == "0-0"
    after_week_1 = store.season_record(season, "2025-09-08T00:00:00Z")
    assert (after_week_1["wins"], after_week_1["losses"]) == (0, 1)
    after_week_3 = store.season_record(season, "2025-09-22T00:00:00Z")
    assert after_week_3["record"] == "1-2"
    final = store.season_record(season)
    assert final["record"] == "14-3"
    assert len(final["lineage"]) == 17


def test_claim_requires_evidence(store: Store) -> None:
    game = store.add_entity("Game", "Game", {}, "test")
    question = store.add_question("result", "Game", "Enum[W,L]", {}, "test")
    with pytest.raises(EpiqError, match="Evidence not found"):
        store.assert_claim(game, question, "W", "2025-01-01", "evd_missing", "test")


def test_claim_is_idempotent(store: Store) -> None:
    game = store.add_entity("Game", "Game", {}, "test")
    question = store.add_question("result", "Game", "Enum[W,L]", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "Result", "2025-01-02", "Game won.", "test"
    )
    first = store.assert_claim(game, question, "W", "2025-01-01", evidence, "test")
    second = store.assert_claim(game, question, "W", "2025-01-01", evidence, "test")
    assert first == second
    assert [event["event_type"] for event in store.history()].count("claim.assert") == 1


def test_retraction_changes_current_view_but_preserves_history(store: Store) -> None:
    game = store.add_entity("Game", "Game", {}, "test")
    question = store.add_question("result", "Game", "Enum[W,L]", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "Result", "2025-01-02", "Game won.", "test"
    )
    claim = store.assert_claim(game, question, "W", "2025-01-01", evidence, "test")
    store.close_claim(claim, "retracted", "Score was provisional", "reviewer")
    events = store.history()
    assert [event["event_type"] for event in events][-1] == "claim.retracted"
    assert any(event["payload"].get("claim_id") == claim for event in events)


def test_validity_end_expires_valid_time_without_retracting_claim(store: Store) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("ceo", "Company", "String", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/acme", "Acme", "2026-08-17", "Ada was CEO in 2024.", "test"
    )
    claim = store.assert_claim(company, "ceo", "Ada", "2024-01-01", evidence, "test")
    learned_at = next(
        event["recorded_at"] for event in store.history() if event["event_type"] == "claim.assert"
    )
    store.end_claim_validity(claim, "2025-01-01", "Leadership changed", "reviewer")
    assert store.history()[-1]["event_type"] == "claim.validity_end"
    old = store.matrix("Company", valid_at="2024-06-01")["rows"][0]["cells"]["ceo"]
    current = store.matrix("Company", valid_at="2025-06-01")["rows"][0]["cells"]["ceo"]
    assert old["value"] == "Ada"
    assert current["state"] == "Unasked"
    before_epiq_learned_the_end = store.matrix(
        "Company", known_at=learned_at, valid_at="2025-06-01"
    )["rows"][0]["cells"]["ceo"]
    assert before_epiq_learned_the_end["value"] == "Ada"
    with store.connect() as connection:
        status = connection.execute(
            "SELECT status FROM claims WHERE claim_id=?", (claim,)
        ).fetchone()[0]
    assert status == "asserted"


def test_evidence_assessment_surfaces_quality_without_erasing_lineage(store: Store) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("active", "Company", "Bool", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/acme", "Acme", "2026-08-17", "Acme is active.", "test"
    )
    store.assert_claim(company, "active", True, "2026-08-17", evidence, "test")
    store.assess_evidence(evidence, "disputed", "Page may describe a different Acme", "reviewer")
    lineage = store.matrix("Company")["rows"][0]["cells"]["active"]["lineage"]
    assert lineage[0]["evidence_status"] == "disputed"
    assert "different Acme" in lineage[0]["evidence_assessment_reason"]
    store.assess_evidence(evidence, "accepted", "Identity independently verified", "reviewer")
    lineage = store.matrix("Company")["rows"][0]["cells"]["active"]["lineage"]
    assert lineage[0]["evidence_status"] == "accepted"
    assert [item["status"] for item in store.evidence_assessments(evidence)] == [
        "disputed",
        "accepted",
    ]


def test_structured_query_dossier_timeline_and_delta_reports(store: Store) -> None:
    acme = store.add_entity("Company", "Acme", {}, "test")
    beta = store.add_entity("Company", "Beta", {}, "test")
    store.add_question("funding", "Company", "Quantity[USD]", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/funding", "Funding", "2026-08-17", "Acme raised $12m.", "test"
    )
    claim = store.assert_claim(acme, "funding", 12_000_000, "2026-01-01", evidence, "test")
    result = store.query_rows(
        "Company", [{"question": "funding", "op": "gte", "value": 10_000_000}]
    )
    assert [row["entity_id"] for row in result["rows"]] == [acme]
    assert beta not in {row["entity_id"] for row in result["rows"]}
    dossier = store.dossier("Acme")
    assert dossier["entity"]["cells"]["funding"]["value"] == 12_000_000
    assert any(event["payload"].get("claim_id") == claim for event in dossier["events"])
    timeline = store.timeline("Company", "funding")
    assert timeline["observations"][0]["claim_id"] == claim
    assert timeline["observations"][0]["sources"][0]["evidence_id"] == evidence

    first_delta = store.delta_report("reporter")
    assert first_delta["event_count"] > 0
    second_delta = store.delta_report("reporter")
    assert second_delta["event_count"] == 0
    store.add_entity("Company", "Gamma", {}, "test")
    third_delta = store.delta_report("reporter")
    assert [event["event_type"] for event in third_delta["events"]] == ["entity.create"]


def test_declarative_apply_is_atomic_idempotent_and_versions_changed_schema(store: Store) -> None:
    document = {
        "entity_kinds": ["Person", "Work"],
        "entities": [
            {"kind": "Person", "name": "Ada"},
            {"kind": "Work", "name": "Notes"},
        ],
        "questions": [{"name": "author", "subject_kind": "Work", "value_type": "Ref[Person]"}],
        "aliases": [{"entity": "Ada", "alias": "Ada Lovelace"}],
    }
    first = store.apply_document(document, "deployer")
    assert {item["status"] for item in first["entities"]} == {"created"}
    before = len(store.history())
    second = store.apply_document(document, "deployer")
    assert len(store.history()) == before
    assert {item["status"] for item in second["entities"]} == {"unchanged"}
    assert second["questions"][0]["status"] == "unchanged"

    changed = {
        **document,
        "questions": [
            {
                "name": "author",
                "subject_kind": "Work",
                "value_type": "Ref[Person]",
                "definition": {"cardinality": "many"},
            }
        ],
    }
    result = store.apply_document(changed, "deployer")
    assert result["questions"][0]["status"] == "versioned"

    failing = {
        "entities": [{"kind": "Work", "name": "Rolled Back"}],
        "questions": [{"name": "bad", "subject_kind": "Work", "value_type": "BadType"}],
    }
    before = len(store.history())
    with pytest.raises(EpiqError):
        store.apply_document(failing, "deployer")
    assert len(store.history()) == before
    assert all(row["name"] != "Rolled Back" for row in store.matrix("Work")["rows"])


def test_reference_projection_queries_and_bidirectional_traversal(store: Store) -> None:
    person = store.add_entity("Person", "Paul Graham", {}, "test")
    work = store.add_entity("Work", "An Essay", {}, "test")
    store.add_question("author", "Work", "Ref[Person]", {}, "test")
    store.add_question("topic", "Work", "String", {"cardinality": "many"}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/essay", "Essay", "2026-08-17", "An essay by Paul Graham.", "test"
    )
    store.assert_claim(work, "author", "Paul Graham", "2026-08-17", evidence, "test")
    store.assert_claim(work, "topic", "Startups", "2026-08-17", evidence, "test")
    store.assert_claim(work, "topic", "Programming", "2026-08-17", evidence, "test")
    cell = store.matrix("Work")["rows"][0]["cells"]["author"]
    assert cell["value"] == person
    assert cell["display_value"] == {"entity_id": person, "name": "Paul Graham", "kind": "Person"}
    assert (
        store.query_rows("Work", [{"question": "author", "op": "eq", "value": "Paul Graham"}])[
            "query"
        ]["matched"]
        == 1
    )
    assert (
        store.query_rows(
            "Work",
            [{"question": "topic", "op": "contains_all", "value": ["Startups", "Programming"]}],
        )["query"]["matched"]
        == 1
    )
    incoming = store.related("Paul Graham", "author", "incoming")
    assert incoming["edges"][0]["from"]["name"] == "An Essay"
    outgoing = store.related("An Essay", "author", "outgoing")
    assert outgoing["edges"][0]["to"]["name"] == "Paul Graham"


def test_batch_operation_actor_and_non_web_source_type_are_preserved(store: Store) -> None:
    person = store.add_entity("Person", "Candidate", {}, "test")
    store.add_question("rating", "Person", "Probability", {}, "test")
    results = store.write_batch(
        [
            {
                "op": "evidence.add",
                "ref": "interview",
                "source_type": "interview",
                "url": "urn:epiq:interview:1",
                "title": "Interview",
                "retrieved_at": "2026-08-17",
                "excerpt": "The interviewer assigned 0.8.",
                "actor": "interviewer:one",
            },
            {
                "op": "claim.assert",
                "subject": person,
                "question": "rating",
                "value": 0.8,
                "valid_from": "2026-08-17",
                "evidence_refs": ["interview"],
                "actor": "interviewer:one",
            },
        ],
        "agent:importer",
    )
    evidence_id = results[0]["evidence_id"]
    events = store.history()
    evidence_event = next(
        event for event in events if event["payload"].get("evidence_id") == evidence_id
    )
    claim_event = next(
        event for event in events if event["payload"].get("claim_id") == results[1]["claim_id"]
    )
    assert evidence_event["actor"] == claim_event["actor"] == "interviewer:one"
    assert evidence_event["payload"]["submitted_by"] == "agent:importer"
    lineage = store.matrix("Person")["rows"][0]["cells"]["rating"]["lineage"]
    assert lineage[0]["source"]["source_type"] == "interview"


def test_project_bundle_round_trip_and_checksum_rejection(store: Store, tmp_path: Path) -> None:
    store.add_entity("Company", "Acme", {}, "test")
    bundle = store.export_bundle(tmp_path / "project.epiq")
    restored = Store.import_bundle(bundle, tmp_path / "restored.sqlite")
    assert restored.overview() == store.overview()
    assert restored.matrix("Company")["rows"][0]["name"] == "Acme"
    assert restored.doctor()["ok"] is True

    corrupt = tmp_path / "corrupt.epiq"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(corrupt, "w") as target:
        target.writestr("manifest.json", source.read("manifest.json"))
        target.writestr("project.sqlite", source.read("project.sqlite") + b"corrupt")
    with pytest.raises(EpiqError) as error:
        Store.import_bundle(corrupt, tmp_path / "bad.sqlite")
    assert error.value.code == "bundle_checksum_mismatch"


def test_question_challenge_captures_category_error_and_resolution(store: Store) -> None:
    boat = store.add_entity("BoatModel", "RS Quest", {}, "test")
    question = store.add_question("has_spinnaker", "BoatModel", "Bool", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/quest",
        "RS Quest options",
        "2026-08-16",
        "The asymmetric spinnaker is optional.",
        "test",
    )
    proposal = {
        "name": "spinnaker_availability",
        "value_type": "Enum[standard,optional,unavailable,unknown]",
        "definition": {"label": "Spinnaker availability"},
    }
    challenge = store.challenge_question(
        question,
        "modal_ambiguity",
        "The Boolean conflates actual equipment with optional capability.",
        "reviewer",
        boat,
        [evidence],
        proposal,
    )
    assert store.question_challenges("has_spinnaker", "open") == [
        {
            "challenge_id": challenge,
            "question_id": question,
            "question_name": "has_spinnaker",
            "problem": "modal_ambiguity",
            "explanation": "The Boolean conflates actual equipment with optional capability.",
            "example_entity_id": boat,
            "example_entity_name": "RS Quest",
            "evidence_ids": [evidence],
            "proposed_replacement": proposal,
            "status": "open",
            "resolution": None,
        }
    ]
    matrix = store.matrix("BoatModel")
    assert matrix["rows"][0]["cells"]["has_spinnaker"]["state"] == "Unasked"
    assert matrix["questions"][0]["schema_state"] == "challenged"
    assert matrix["questions"][0]["open_challenges"][0]["challenge_id"] == challenge
    store.resolve_question_challenge(
        challenge,
        "resolved",
        "Replace it with availability and equipped-on-instance questions.",
        "reviewer",
    )
    resolved = store.question_challenges(status="resolved")[0]
    assert resolved["resolution"].startswith("Replace it")
    assert store.matrix("BoatModel")["questions"][0]["schema_state"] == "active"
    assert [event["event_type"] for event in store.history()][-2:] == [
        "question.challenge",
        "question.challenge_resolved",
    ]


def test_question_challenge_validates_taxonomy_and_replacement(store: Store) -> None:
    store.add_question("has_spinnaker", "BoatModel", "Bool", {}, "test")
    with pytest.raises(EpiqError, match="Unknown question challenge problem"):
        store.challenge_question("has_spinnaker", "bad_type", "Wrong", "test")
    with pytest.raises(EpiqError, match="requires name and value_type"):
        store.challenge_question(
            "has_spinnaker",
            "type_mismatch",
            "Boolean is too narrow",
            "test",
            proposed_replacement={"name": "availability"},
        )


def test_completed_search_is_not_confused_with_negative_claim(store: Store) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    store.add_question("model_control", "Company", "Enum[selectable]", {}, "test")
    store.record_not_found(
        company,
        "model_control",
        "site:example.test choose LLM",
        "No public model-selection control found.",
        "agent:researcher",
    )
    cell = store.matrix("Company", ["model_control"])["rows"][0]["cells"]["model_control"]
    assert cell["state"] == "NotFound"
    assert cell["values"] == []
    assert "No public" in cell["research"]["notes"]

    task_id = cell["research"]["task_id"]
    feedback = store.record_research_feedback(
        task_id,
        "The consulted list is exhaustive.",
        "Closed authoritative lists can support negative values.",
        "human:reviewer",
    )
    assert feedback["subject_id"] == company
    assert store.history()[-1]["event_type"] == "research.feedback"


def test_overview_discovers_available_entity_projections(store: Store) -> None:
    store.add_entity("Company", "Example", {}, "test")
    store.add_entity("Product", "Widget", {}, "test")
    store.add_question("pricing", "Company", "Json", {}, "test")
    overview = store.overview()

    assert overview["project"]["name"] == "test"
    assert overview["entity_kinds"] == [
        {"kind": "Company", "entities": 1, "questions": 1},
        {"kind": "Product", "entities": 1, "questions": 0},
    ]


def test_empty_entity_kind_exists_as_a_sheet_without_a_fake_row(store: Store) -> None:
    assert store.add_entity_kind("Investor", "test") == "Investor"
    assert store.add_entity_kind("Investor", "retry") == "Investor"

    assert store.overview()["entity_kinds"] == [{"kind": "Investor", "entities": 0, "questions": 0}]
    assert store.matrix("Investor")["rows"] == []
    assert [event["event_type"] for event in store.history()].count("entity_kind.define") == 1


@pytest.mark.parametrize("value", [0.73, 0, 1, 42.5])
def test_float_question_accepts_finite_json_numbers(store: Store, value: float) -> None:
    entity = store.add_entity("Forecast", "Example", {}, "test")
    store.add_question("probability", "Forecast", "Float", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/forecast", "Forecast", "2026-08-16", "Probability estimate.", "test"
    )

    store.assert_claim(entity, "probability", value, "2026-08-16", evidence, "test")
    assert store.matrix("Forecast")["rows"][0]["cells"]["probability"]["value"] == value


@pytest.mark.parametrize("value", [True, "0.73", float("inf"), float("nan")])
def test_float_question_rejects_non_numeric_or_non_finite_values(
    store: Store, value: object
) -> None:
    entity = store.add_entity("Forecast", "Example", {}, "test")
    store.add_question("probability", "Forecast", "Float", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/forecast", "Forecast", "2026-08-16", "Probability estimate.", "test"
    )

    with pytest.raises(EpiqError, match="Expected finite Float"):
        store.assert_claim(entity, "probability", value, "2026-08-16", evidence, "test")


def test_string_question_is_distinct_from_json(store: Store) -> None:
    entity = store.add_entity("Company", "Example", {}, "test")
    store.add_question("summary", "Company", "String", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/about", "About", "2026-08-16", "A plain-text summary.", "test"
    )

    store.assert_claim(entity, "summary", "A plain-text summary.", "2026-08-16", evidence, "test")
    with pytest.raises(EpiqError, match="Expected String"):
        store.assert_claim(
            entity, "summary", {"text": "structured"}, "2026-08-16", evidence, "test"
        )


def test_existing_database_migrates_primary_evidence_to_schema_two(store: Store) -> None:
    entity = store.add_entity("Company", "Example", {}, "test")
    store.add_question("summary", "Company", "String", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "About", "2026-08-16", "Summary.", "test"
    )
    store.assert_claim(entity, "summary", "Summary.", "2026-08-16", evidence, "test")
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE claim_inputs")
        connection.execute("DROP TABLE derivations")
        connection.execute("DROP TABLE claim_evidence")
        connection.execute("UPDATE meta SET value='1' WHERE key='schema_version'")

    cell = store.matrix("Company")["rows"][0]["cells"]["summary"]
    assert cell["lineage"][0]["evidence_id"] == evidence
    assert store.overview()["project"]["schema_version"] == "13"


def test_compound_entity_identity_is_idempotent(store: Store) -> None:
    identity = {"forecaster": "Alice", "issued_at": "2026-08-17T09:00:00Z"}
    first = store.add_entity(
        "Forecast", "Alice forecast", {}, "test", role="observation", identity=identity
    )
    second = store.add_entity(
        "Forecast", "A different display name", {}, "test", role="observation", identity=identity
    )
    assert second == first
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM entities WHERE entity_id=?", (first,)).fetchone()
    assert row["role"] == "observation"
    assert json.loads(row["identity_json"]) == identity


def test_general_derivation_persists_formula_inputs_and_evidence(store: Store) -> None:
    event = store.add_entity("Event", "Rain", {}, "test")
    store.add_question("forecast", "Event", "Probability", {"cardinality": "many"}, "test")
    store.add_question("ensemble", "Event", "Probability", {}, "test")
    inputs = []
    for index, value in enumerate((0.3, 0.5)):
        _, evidence = store.add_evidence(
            f"https://example.test/{index}", "Forecast", "2026-08-17", str(value), "test"
        )
        inputs.append(store.assert_claim(event, "forecast", value, "2026-08-17", evidence, "test"))
    derived = store.derive_claim(
        event,
        "ensemble",
        "weighted_avg",
        inputs,
        "2026-08-17",
        "agent:ensemble",
        {"weights": [1, 3]},
    )
    cell = store.matrix("Event")["rows"][0]["cells"]["ensemble"]
    assert cell["value"] == pytest.approx(0.45)
    derivation = cell["lineage"][0]["derivation"]
    assert derivation == {
        "operation": "weighted_avg",
        "parameters": {"weights": [1, 3]},
        "input_claim_ids": inputs,
        "dependencies": [
            {"claim_id": inputs[0], "role": "operand"},
            {"claim_id": inputs[1], "role": "operand"},
        ],
    }
    assert derived == cell["lineage"][0]["claim_id"]


def test_division_formula_materializes_row_relative_values(store: Store) -> None:
    store.add_entity("Company", "Acme", {}, "test")
    store.add_question("revenue", "Company", "Float", {}, "test")
    store.add_question("employees", "Company", "Int", {}, "test")
    store.add_question(
        "revenue_per_employee",
        "Company",
        "Float",
        {
            "formula": {
                "operation": "divide",
                "inputs": ["revenue", "employees"],
                "expression": "=B1/C1",
            }
        },
        "test",
    )
    _, evidence = store.add_evidence(
        "urn:test:accounts", "Accounts", "2026-08-17", "Revenue 120, staff 4", "test"
    )
    store.assert_claim("Acme", "revenue", 120.0, "2026-08-17", evidence, "test")
    store.assert_claim("Acme", "employees", 4, "2026-08-17", evidence, "test")

    result = store.materialize_formulas("Company", "2026-08-17", "human:test")

    assert result["results"] == [
        {
            "subject": "Acme",
            "question": "revenue_per_employee",
            "status": "materialized",
            "claim_id": result["results"][0]["claim_id"],
        }
    ]
    cell = store.matrix("Company")["rows"][0]["cells"]["revenue_per_employee"]
    assert cell["value"] == 30.0
    assert cell["lineage"][0]["derivation"]["operation"] == "divide"


def test_arithmetic_expression_formula_supports_constants_and_parentheses(store: Store) -> None:
    store.add_entity("Startup", "Acme", {}, "test")
    store.add_question("funding", "Startup", "Float", {}, "test")
    store.add_question("founded", "Startup", "Year", {}, "test")
    store.add_question(
        "funding_per_year",
        "Startup",
        "Float",
        {
            "formula": {
                "operation": "expression",
                "inputs": ["funding", "founded"],
                "parameters": {"expression": "x0/(2026-x1)"},
                "expression": "=B1/(2026-C1)",
            }
        },
        "test",
    )
    _, evidence = store.add_evidence(
        "urn:test:startup", "Startup data", "2026-08-17", "Funding 90, founded 2023", "test"
    )
    store.assert_claim("Acme", "funding", 90.0, "2026-08-17", evidence, "test")
    store.assert_claim("Acme", "founded", 2023, "2026-08-17", evidence, "test")

    store.materialize_formulas("Startup", "2026-08-17", "human:test")

    cell = store.matrix("Startup")["rows"][0]["cells"]["funding_per_year"]
    assert cell["value"] == 30.0
    assert cell["lineage"][0]["derivation"]["parameters"] == {
        "expression": "x0/(2026-x1)"
    }


def test_arithmetic_expression_rejects_unsafe_syntax_and_zero_division() -> None:
    with pytest.raises(EpiqError, match="unsupported operation"):
        _evaluate_arithmetic_expression("__import__('os').system('true')", [1.0])

    with pytest.raises(EpiqError, match="division by zero"):
        _evaluate_arithmetic_expression("x0/(2026-x1)", [90.0, 2026.0])


def test_explicit_migration_plan_backup_and_apply(store: Store, tmp_path: Path) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE evidence_assessments")
        connection.execute("DROP TABLE claim_validity_ends")
        connection.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
    plan = store.migration_plan()
    assert plan["pending"] == [
        {"version": 10, "description": "claim validity endings and evidence assessments"},
        {"version": 11, "description": "first-class evidence source types"},
        {
            "version": 12,
            "description": "entity roles, compound identities, and structured source locators",
        },
        {
            "version": 13,
            "description": "typed derivation dependencies and stale-derived-claim detection",
        },
    ]
    backup = tmp_path / "before-v10.sqlite"
    result = store.migrate(backup)
    assert result["before"]["current_version"] == 9
    assert result["after"]["current_version"] == 13
    assert result["after"]["pending"] == []
    with sqlite3.connect(backup) as connection:
        assert (
            connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            == "9"
        )


def test_immutable_record_guards_and_doctor_projection_checks(store: Store) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("active", "Company", "Bool", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/acme", "Acme", "2026-08-17", "Acme is active.", "test"
    )
    claim = store.assert_claim(company, "active", True, "2026-08-17", evidence, "test")
    with (
        sqlite3.connect(store.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="events are immutable"),
    ):
        connection.execute("UPDATE events SET actor='tampered' WHERE seq=1")
    with (
        sqlite3.connect(store.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="evidence is immutable"),
    ):
        connection.execute(
            "UPDATE evidence SET excerpt='tampered' WHERE evidence_id=?", (evidence,)
        )
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM claim_evidence WHERE claim_id=?", (claim,))
    report = store.doctor()
    assert report["ok"] is False
    assert any(item["code"] == "claim_evidence_projection_mismatch" for item in report["findings"])


def test_newer_schema_is_rejected_without_mutation(store: Store) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(EpiqError) as error:
        store.overview()
    assert error.value.code == "schema_too_new"


def test_entity_alias_merge_and_retirement_preserve_identity_history(store: Store) -> None:
    canonical = store.add_entity("Company", "Acme", {}, "test")
    duplicate = store.add_entity("Company", "Acme Incorporated", {}, "test")
    question = store.add_question("status", "Company", "String", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/acme", "Acme", "2026-08-17", "Acme is active.", "test"
    )
    claim = store.assert_claim(duplicate, question, "active", "2026-08-17", evidence, "test")

    store.add_entity_alias(canonical, "ACME Corp.", "test")
    assert store.assert_claim("acme corp.", question, "operating", "2026-08-17", evidence, "test")
    store.merge_entities(duplicate, canonical, "Duplicate identity", "reviewer")
    matrix = store.matrix("Company")
    assert [row["name"] for row in matrix["rows"]] == ["Acme"]
    assert matrix["rows"][0]["merged_entity_ids"] == [duplicate]
    assert matrix["rows"][0]["aliases"] == ["ACME Corp."]
    assert claim in {
        lineage["claim_id"] for lineage in matrix["rows"][0]["cells"]["status"]["lineage"]
    }

    store.set_entity_visibility(canonical, False, "No longer in scope", "reviewer")
    assert store.matrix("Company")["rows"] == []
    with pytest.raises(EpiqError) as retired:
        store.assert_claim(canonical, question, "active", "2026-08-17", evidence, "test")
    assert retired.value.code == "entity_retired"
    store.set_entity_visibility(duplicate, True, "Back in scope", "reviewer")
    assert store.matrix("Company")["rows"][0]["name"] == "Acme"


def test_entity_identity_rejects_normalized_duplicates_and_invalid_merges(store: Store) -> None:
    first = store.add_entity("Person", "Paul  Graham", {}, "test")
    with pytest.raises(EpiqError) as duplicate:
        store.add_entity("Person", " paul graham ", {}, "test")
    assert duplicate.value.code == "duplicate_entity"
    company = store.add_entity("Company", "Example", {}, "test")
    with pytest.raises(EpiqError) as mismatch:
        store.merge_entities(first, company, "Not actually duplicates", "test")
    assert mismatch.value.code == "entity_kind_mismatch"


def test_typed_entity_reference_resolves_alias_and_survives_merge(store: Store) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    old_company = store.add_entity("Company", "Acme Holdings", {}, "test")
    person = store.add_entity("Person", "Ada", {}, "test")
    store.add_entity_alias(company, "Acme Corp", "test")
    store.add_question("employer", "Person", "Ref[Company]", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/ada", "Ada", "2026-08-17", "Ada works at Acme.", "test"
    )
    store.assert_claim(person, "employer", "Acme Corp", "2026-08-17", evidence, "test")
    assert store.matrix("Person")["rows"][0]["cells"]["employer"]["value"] == company

    store.merge_entities(company, old_company, "Corporate identity cleanup", "test")
    # Existing references retain their immutable target ID; resolution follows the redirect.
    with store.connect() as connection:
        assert store._resolve_entity(connection, company)["entity_id"] == old_company
    with pytest.raises(EpiqError) as wrong_kind:
        store.assert_claim(person, "employer", person, "2026-08-17", evidence, "test")
    assert wrong_kind.value.code == "reference_type_mismatch"


def test_quantity_type_encodes_unit_and_requires_finite_number(store: Store) -> None:
    town = store.add_entity("Town", "Truro", {}, "test")
    store.add_question("area", "Town", "Quantity[km^2]", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/truro", "Truro", "2026-08-17", "Area is 68.2 km².", "test"
    )
    store.assert_claim(town, "area", 68.2, "2026-08-17", evidence, "test")
    assert store.matrix("Town")["rows"][0]["cells"]["area"]["value"] == 68.2
    with pytest.raises(EpiqError, match="finite quantity"):
        store.assert_claim(town, "area", "68.2", "2026-08-17", evidence, "test")


@pytest.mark.parametrize(
    ("value_type", "value"),
    [
        ("Date", "2026-08-17"),
        ("DateTime", "2026-08-17T12:30:00Z"),
        ("Year", 2026),
        ("Interval[Date]", {"start": "2026-01-01", "end": "2026-08-17"}),
    ],
)
def test_temporal_value_types(store: Store, value_type: str, value: object) -> None:
    item = store.add_entity("Item", value_type, {}, "test")
    store.add_question("when", "Item", value_type, {}, "test")
    _, evidence = store.add_evidence(
        f"https://example.test/{value_type}", "When", "2026-08-17", "Temporal value.", "test"
    )
    store.assert_claim(item, "when", value, "2026-08-17", evidence, "test")
    assert store.matrix("Item")["rows"][0]["cells"]["when"]["value"] == value


@pytest.mark.parametrize(
    ("value_type", "value"),
    [
        ("Date", "08/17/2026"),
        ("DateTime", "2026-08-17T12:30:00"),
        ("Year", "2026"),
        ("Interval[Date]", {"start": "2026-08-17", "end": "2026-01-01"}),
    ],
)
def test_temporal_value_types_reject_malformed_values(
    store: Store, value_type: str, value: object
) -> None:
    item = store.add_entity("Item", value_type, {}, "test")
    store.add_question("when", "Item", value_type, {}, "test")
    _, evidence = store.add_evidence(
        f"https://example.test/{value_type}", "When", "2026-08-17", "Temporal value.", "test"
    )
    with pytest.raises(EpiqError) as error:
        store.assert_claim(item, "when", value, "2026-08-17", evidence, "test")
    assert error.value.code == "value_type_error"


def test_url_value_type_accepts_only_absolute_http_urls(store: Store) -> None:
    item = store.add_entity("Company", "Acorn", {}, "test")
    store.add_question("website", "Company", "URL", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/source",
        "Company profile",
        "2026-08-17",
        "Official website is https://acorn.example/about.",
        "test",
    )

    store.assert_claim(
        item,
        "website",
        "https://acorn.example/about?team=1",
        "2026-08-17",
        evidence,
        "test",
    )
    assert store.matrix("Company")["rows"][0]["cells"]["website"]["value"] == (
        "https://acorn.example/about?team=1"
    )

    for invalid in ["acorn.example", "/about", "javascript:alert(1)", 42]:
        with pytest.raises(EpiqError) as error:
            store.assert_claim(
                item,
                "website",
                invalid,
                "2026-08-17",
                evidence,
                "test",
            )
        assert error.value.code == "value_type_error"


def test_claim_proposals_are_invisible_until_atomically_approved(store: Store) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("employees", "Company", "Int", {}, "test")
    store.add_question("active", "Company", "Bool", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/acme",
        "Acme",
        "2026-08-17",
        "Acme has 12 staff and is active.",
        "test",
    )
    first = store.propose_claim(
        company, "employees", 12, "2026-08-17", [evidence], "agent:test", rationale="Official page"
    )
    second = store.propose_claim(company, "active", True, "2026-08-17", [evidence], "agent:test")
    assert store.matrix("Company")["rows"][0]["cells"]["employees"]["state"] == "Unasked"
    assert [item["proposal_id"] for item in store.claim_proposals()] == [first, second]

    reviewed = store.review_claim_proposals(
        [first, second], "approved", "Sources verified", "human:reviewer"
    )
    assert all(item["claim_id"] for item in reviewed)
    row = store.matrix("Company")["rows"][0]
    assert row["cells"]["employees"]["value"] == 12
    assert row["cells"]["active"]["value"] is True
    assert store.claim_proposals() == []
    assert len(store.claim_proposals("approved")) == 2


def test_rejected_proposal_never_becomes_claim(store: Store) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("active", "Company", "Bool", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/acme", "Acme", "2026-08-17", "Possibly active.", "test"
    )
    proposal = store.propose_claim(company, "active", True, "2026-08-17", [evidence], "agent")
    result = store.review_claim_proposals(
        [proposal], "rejected", "Evidence is equivocal", "reviewer"
    )
    assert result == [{"proposal_id": proposal, "status": "rejected", "claim_id": None}]
    assert store.matrix("Company")["rows"][0]["cells"]["active"]["state"] == "Unasked"


def test_bulk_claim_assertion_rolls_back_entire_batch_on_late_error(store: Store) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("employees", "Company", "Int", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/acme", "Acme", "2026-08-17", "Acme has 12 staff.", "test"
    )
    items = [
        {
            "subject": company,
            "question": "employees",
            "value": 12,
            "valid_from": "2026-08-17",
            "evidence_ids": [evidence],
        },
        {
            "subject": company,
            "question": "employees",
            "value": "twelve",
            "valid_from": "2026-08-17",
            "evidence_ids": [evidence],
        },
    ]
    before = len(store.history())
    with pytest.raises(EpiqError, match="batch item 1"):
        store.assert_claims_bulk(items, "agent:test")
    assert len(store.history()) == before
    assert store.matrix("Company")["rows"][0]["cells"]["employees"]["state"] == "Unasked"


def test_relationship_change_set_is_atomic_and_idempotent(store: Store) -> None:
    paper = store.add_entity("Paper", "Markets", {}, "test")
    question = store.add_question(
        "authors", "Paper", "Ref[Author]", {"cardinality": "one"}, "test"
    )
    finding = {
        "suggestion_id": "rel_ada",
        "subject_entity_id": paper,
        "target_name": "Ada",
        "source_type": "web",
        "source_url": "https://example.test/markets",
        "source_title": "Markets title page",
        "retrieved_at": "2026-08-17",
        "excerpt": "Markets was written by Ada.",
        "confidence": "high",
    }
    change_set = {
        "change_set_id": "cs_markets_authors",
        "kind": "relationship_cardinality",
        "predecessor_question_id": question,
        "findings": [finding],
    }

    first = store.apply_change_set(change_set, "human:reviewer")
    second = store.apply_change_set(change_set, "human:reviewer")
    assert second == first
    assert first["accepted_relationships"] == 1
    matrix = store.matrix("Paper")
    assert matrix["questions"][0]["definition"]["cardinality"] == "many"
    assert matrix["rows"][0]["cells"]["authors"]["references"][0]["name"] == "Ada"
    assert [event["event_type"] for event in store.history()].count("change_set.apply") == 1


def test_relationship_change_set_rolls_back_schema_and_writes_on_late_error(
    store: Store,
) -> None:
    paper = store.add_entity("Paper", "Markets", {}, "test")
    question = store.add_question(
        "authors", "Paper", "Ref[Author]", {"cardinality": "one"}, "test"
    )
    before = len(store.history())
    change_set = {
        "change_set_id": "cs_broken_authors",
        "kind": "relationship_cardinality",
        "predecessor_question_id": question,
        "findings": [
            {
                "suggestion_id": "rel_ada",
                "subject_entity_id": paper,
                "target_name": "Ada",
                "source_type": "web",
                "source_url": "https://example.test/markets",
                "source_title": "Markets title page",
                "retrieved_at": "2026-08-17",
                "excerpt": "Markets was written by Ada.",
            },
            {
                "suggestion_id": "rel_missing",
                "subject_entity_id": "ent_missing",
                "target_name": "Grace",
                "source_type": "web",
                "source_url": "https://example.test/missing",
                "source_title": "Missing paper",
                "retrieved_at": "2026-08-17",
                "excerpt": "A missing paper was written by Grace.",
            },
        ],
    }

    with pytest.raises(EpiqError, match="Entity not found"):
        store.apply_change_set(change_set, "human:reviewer")
    assert len(store.history()) == before
    assert store.matrix("Paper")["questions"][0]["question_id"] == question
    assert store.matrix("Author")["rows"] == []


def test_atomic_write_batch_links_new_evidence_and_rolls_it_back_on_claim_error(
    store: Store,
) -> None:
    company = store.add_entity("Company", "Acme", {}, "test")
    store.add_question("employees", "Company", "Int", {}, "test")
    operations = [
        {
            "op": "evidence.add",
            "ref": "staff_page",
            "url": "https://example.test/staff",
            "title": "Staff",
            "retrieved_at": "2026-08-17",
            "excerpt": "Acme has twelve employees.",
        },
        {
            "op": "claim.assert",
            "subject": company,
            "question": "employees",
            "value": 12,
            "valid_from": "2026-08-17",
            "evidence_refs": ["staff_page"],
        },
    ]
    results = store.write_batch(operations, "agent:test")
    assert results[1]["claim_id"]
    evidence_id = results[0]["evidence_id"]
    lineage = store.matrix("Company")["rows"][0]["cells"]["employees"]["lineage"]
    assert lineage[0]["evidence_id"] == evidence_id

    failing = [
        {
            "op": "evidence.add",
            "ref": "bad_page",
            "url": "https://example.test/bad",
            "title": "Bad",
            "retrieved_at": "2026-08-17",
            "excerpt": "Malformed interpretation.",
        },
        {
            "op": "claim.assert",
            "subject": company,
            "question": "employees",
            "value": "twelve",
            "valid_from": "2026-08-17",
            "evidence_refs": ["bad_page"],
        },
    ]
    before = store.doctor()["counts"]
    with pytest.raises(EpiqError, match="Write batch item 1"):
        store.write_batch(failing, "agent:test")
    after = store.doctor()["counts"]
    assert after["evidence"] == before["evidence"]
    assert after["events"] == before["events"]


def test_dynamic_field_without_freshness_window_is_unknown(store: Store) -> None:
    company = store.add_entity("Company", "Acorn", {}, "test")
    store.add_question("status", "Company", "String", {"volatility": "dynamic"}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/status", "Status", "2026-08-17", "Acorn is active.", "test"
    )
    store.assert_claim(company, "status", "active", "2026-08-17", evidence, "test")
    temporal = store.matrix("Company")["rows"][0]["cells"]["status"]["temporal"]
    assert temporal["freshness"] == "unknown"
    assert temporal["age_days"] is None


def test_question_retirement_hides_projection_but_preserves_and_restores_history(
    store: Store,
) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    question = store.add_question("rating", "Company", "Float", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/rating", "Rating", "2026-08-17", "Rated 4.8.", "test"
    )
    claim = store.assert_claim(company, question, 4.8, "2026-08-17", evidence, "test")

    store.set_question_visibility(question, False, "Wrong field semantics", "reviewer")
    assert store.matrix("Company")["questions"] == []
    assert store.matrix("Company", ["rating"])["questions"] == []
    assert store.overview()["entity_kinds"][0]["questions"] == 0
    assert any(
        event["event_type"] == "question.retire" and event["payload"]["question_id"] == question
        for event in store.history()
    )
    with pytest.raises(EpiqError) as retired:
        store.assert_claim(company, question, 4.9, "2026-08-17", evidence, "test")
    assert retired.value.code == "question_retired"

    store.set_question_visibility(question, True, "Needed after redesign", "reviewer")
    cell = store.matrix("Company")["rows"][0]["cells"]["rating"]
    assert cell["value"] == 4.8
    assert cell["lineage"][0]["claim_id"] == claim
    assert [event["event_type"] for event in store.history()][-1] == "question.restore"


def test_question_split_is_atomic_and_records_executable_lineage(store: Store) -> None:
    store.add_entity("Boat", "Quest", {}, "test")
    old = store.add_question("has_spinnaker", "Boat", "Bool", {}, "test")
    successors = store.evolve_question(
        old,
        [
            {
                "name": "spinnaker_available",
                "value_type": "Enum[standard,optional,unavailable,unknown]",
                "definition": {"label": "Spinnaker availability"},
            },
            {
                "name": "spinnaker_installed",
                "value_type": "Bool",
                "definition": {"label": "Spinnaker installed on this boat"},
            },
        ],
        "splits",
        "The Boolean conflated capability and current configuration",
        "reviewer",
    )
    assert [item["name"] for item in store.matrix("Boat")["questions"]] == [
        "spinnaker_available",
        "spinnaker_installed",
    ]
    lineage = store.question_lineage(old)
    assert [item["question_id"] for item in lineage["successors"]] == successors
    assert all(item["relationship"] == "splits" for item in lineage["successors"])
    assert store.question_lineage(successors[0])["predecessors"][0]["question_id"] == old
    assert store.history()[-1]["event_type"] == "question.evolve"


def test_invalid_question_evolution_rolls_back_created_successors(store: Store) -> None:
    old = store.add_question("ambiguous", "Company", "Bool", {}, "test")
    before = len(store.history())
    with pytest.raises(EpiqError, match="Unknown or malformed"):
        store.evolve_question(
            old,
            [
                {"name": "valid_first", "value_type": "String"},
                {"name": "bad_second", "value_type": "Nonsense"},
            ],
            "splits",
            "Fix ambiguity",
            "reviewer",
        )
    assert len(store.history()) == before
    with store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM questions WHERE name IN ('valid_first','bad_second')"
            ).fetchone()[0]
            == 0
        )


def test_agent_jobs_persist_replaceable_operational_state(store: Store) -> None:
    job = {
        "job_id": "job_test",
        "created_at": "2026-08-16T12:00:00Z",
        "status": "queued",
        "messages": [],
    }
    store.save_agent_job(job)
    job["status"] = "completed"
    job["messages"].append({"at": "2026-08-16T12:01:00Z", "message": "done"})
    store.save_agent_job(job)

    assert store.agent_jobs() == [job]


def test_evidence_canonicalizes_urls_and_deduplicates_tracking_variants(store: Store) -> None:
    assert (
        canonicalize_url(
            "HTTPS://WWW.Example.test:443/pricing?utm_source=news&plan=team&gclid=x#details"
        )
        == "https://example.test/pricing?plan=team"
    )
    first = store.add_evidence(
        "https://www.example.test/pricing?plan=team&utm_campaign=launch",
        "Pricing",
        "2026-08-16",
        "  Team plan.\r\n",
        "test",
    )
    second = store.add_evidence(
        "https://example.test/pricing?utm_medium=email&plan=team#top",
        "Same page",
        "2026-08-17",
        "Team plan.",
        "test",
    )
    assert second == first
    assert [event["event_type"] for event in store.history()].count("evidence.add") == 1


def test_dynamic_question_surfaces_stale_as_of_and_source_dates(store: Store) -> None:
    person = store.add_entity("Person", "Example", {}, "test")
    store.add_question(
        "residence",
        "Person",
        "String",
        {"volatility": "dynamic", "freshness_days": 90},
        "test",
    )
    _, evidence = store.add_evidence(
        "https://example.test/2019-profile",
        "Old profile",
        "2026-08-16",
        "Example lived in Boston.",
        "test",
        published_at="2019-05-01",
    )
    store.assert_claim(person, "residence", "Boston", "2019-05-01", evidence, "test")

    cell = store.matrix("Person")["rows"][0]["cells"]["residence"]
    assert cell["temporal"]["freshness"] == "stale"
    assert cell["temporal"]["as_of"] == "2019-05-01"
    assert cell["lineage"][0]["source"]["published_at"] == "2019-05-01"
    assert cell["lineage"][0]["source"]["retrieved_at"] == "2026-08-16"


def test_duplicate_entity_rolls_back_its_event(store: Store) -> None:
    store.add_entity("Company", "Example", {}, "test")
    before = store.history()

    with pytest.raises(EpiqError) as error:
        store.add_entity("Company", "Example", {}, "test")

    assert error.value.code == "duplicate_entity"
    assert store.history() == before


def test_evidence_is_idempotent(store: Store) -> None:
    first = store.add_evidence(
        "https://example.test/about", "About", "2026-08-16", "Same excerpt.", "agent:a"
    )
    second = store.add_evidence(
        "https://example.test/about", "A changed title", "2026-08-17", "Same excerpt.", "agent:b"
    )

    assert second == first
    assert [event["event_type"] for event in store.history()].count("evidence.add") == 1


def test_reasserting_claim_can_attach_additional_evidence(store: Store) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    store.add_question("summary", "Company", "String", {}, "test")
    evidence = [
        store.add_evidence(
            f"https://example.test/{index}",
            f"Source {index}",
            "2026-08-16",
            f"Excerpt {index}",
            "test",
        )[1]
        for index in range(3)
    ]

    first = store.assert_claim(company, "summary", "Agreed.", "2026-08-16", evidence[0], "test")
    second = store.assert_claim(
        company, "summary", "Agreed.", "2026-08-16", [evidence[0], evidence[1], evidence[1]], "test"
    )
    third = store.assert_claim(
        company, "summary", "Agreed.", "2026-08-16", [evidence[0], evidence[2]], "test"
    )

    assert first == second == third
    cell = store.matrix("Company")["rows"][0]["cells"]["summary"]
    assert [item["evidence_id"] for item in cell["lineage"]] == evidence
    assert [event["event_type"] for event in store.history()].count("claim.assert") == 1
    assert [event["event_type"] for event in store.history()].count("claim.evidence_link") == 2


def test_single_cardinality_conflicts_are_contested(store: Store) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    store.add_question("status", "Company", "Enum[active,closed]", {}, "test")
    evidence = [
        store.add_evidence(f"https://example.test/{value}", value, "2026-08-16", value, "test")[1]
        for value in ("active", "closed")
    ]
    store.assert_claim(company, "status", "active", "2026-01-01", evidence[0], "test")
    store.assert_claim(company, "status", "closed", "2026-08-01", evidence[1], "test")

    cell = store.matrix("Company")["rows"][0]["cells"]["status"]
    assert cell["state"] == "Contested"
    assert set(cell["values"]) == {"active", "closed"}


def test_reassertion_can_attach_new_evidence_without_duplicate_claim(store: Store) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    store.add_question("status", "Company", "String", {}, "test")
    evidence = [
        store.add_evidence(
            f"https://example.test/{index}", f"Source {index}", "2026-08-16", "Active.", "test"
        )[1]
        for index in range(2)
    ]
    claim = store.assert_claim(company, "status", "active", "2026-08-16", evidence[0], "test")
    repeated = store.assert_claim(company, "status", "active", "2026-08-16", evidence, "test")
    assert repeated == claim
    lineage = store.matrix("Company")["rows"][0]["cells"]["status"]["lineage"]
    assert [item["evidence_id"] for item in lineage] == evidence
    assert [event["event_type"] for event in store.history()].count("claim.assert") == 1
    assert [event["event_type"] for event in store.history()].count("claim.evidence_link") == 1


def test_claim_validation_reports_resolution_and_shape_errors(store: Store) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    product = store.add_entity("Product", "Widget", {}, "test")
    store.add_question("employees", "Company", "Int", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "Source", "2026-08-16", "Ten employees.", "test"
    )

    cases = [
        (("missing", "employees", 10, "2026-08-16", evidence, "test"), "entity_not_found"),
        ((company, "missing", 10, "2026-08-16", evidence, "test"), "question_not_found"),
        ((product, "employees", 10, "2026-08-16", evidence, "test"), "subject_type_mismatch"),
        ((company, "employees", 10, "2026-08-16", [], "test"), "evidence_required"),
        (
            (company, "employees", 10, "2026-08-16", evidence, "test", None, "certain"),
            "confidence_error",
        ),
        ((company, "employees", True, "2026-08-16", evidence, "test"), "value_type_error"),
    ]
    for arguments, code in cases:
        with pytest.raises(EpiqError) as error:
            store.assert_claim(*arguments)
        assert error.value.code == code


def test_claim_state_transitions_are_guarded(store: Store) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    store.add_question("status", "Company", "String", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "Source", "2026-08-16", "Active.", "test"
    )
    claim = store.assert_claim(company, "status", "active", "2026-08-16", evidence, "test")

    store.close_claim(claim, "superseded", "New information", "reviewer")
    with pytest.raises(EpiqError) as inactive:
        store.close_claim(claim, "retracted", "Again", "reviewer")
    assert inactive.value.code == "claim_inactive"
    with pytest.raises(EpiqError) as missing:
        store.close_claim("clm_missing", "retracted", "Missing", "reviewer")
    assert missing.value.code == "claim_not_found"
    with pytest.raises(ValueError):
        store.close_claim(claim, "deleted", "Forbidden", "reviewer")


def test_claim_supersede_is_atomic_and_preserves_history(store: Store) -> None:
    company = store.add_entity("Company", "Example", {}, "test")
    store.add_question("status", "Company", "String", {}, "test")
    _, old_evidence = store.add_evidence(
        "https://example.test/old", "Old", "2026-01-01", "Operating.", "test"
    )
    old_claim = store.assert_claim(company, "status", "active", "2026-01-01", old_evidence, "test")

    with pytest.raises(EpiqError):
        store.supersede_claim(
            old_claim,
            "closed",
            "2026-08-01",
            "evd_missing",
            "Closure announced",
            "reviewer",
        )
    assert store.matrix("Company")["rows"][0]["cells"]["status"]["value"] == "active"

    _, new_evidence = store.add_evidence(
        "https://example.test/new", "New", "2026-08-01", "Closed.", "test"
    )
    replacement = store.supersede_claim(
        old_claim,
        "closed",
        "2026-08-01",
        new_evidence,
        "Closure announced",
        "reviewer",
    )
    cell = store.matrix("Company")["rows"][0]["cells"]["status"]
    assert cell["state"] == "Answered"
    assert cell["value"] == "closed"
    supersede = [event for event in store.history() if event["event_type"] == "claim.supersede"]
    assert len(supersede) == 1
    assert supersede[0]["payload"]["claim_id"] == replacement
    assert supersede[0]["payload"]["supersedes_claim_id"] == old_claim


def test_concurrent_writers_do_not_lose_entities_or_events(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite"
    Store(path).initialize("Concurrent")

    def write(index: int) -> str:
        return Store(path).add_entity(
            "Company", f"Company {index}", {"index": index}, f"agent:{index}"
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        entity_ids = list(executor.map(write, range(40)))

    store = Store(path)
    assert len(set(entity_ids)) == 40
    assert len(store.matrix("Company")["rows"]) == 40
    assert [event["event_type"] for event in store.history()].count("entity.create") == 40
