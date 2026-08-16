import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from epiq.demo import load_patriots
from epiq.errors import EpiqError
from epiq.store import Store


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
    assert store.overview()["project"]["schema_version"] == "2"


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
        store.add_evidence(
            f"https://example.test/{value}", value, "2026-08-16", value, "test"
        )[1]
        for value in ("active", "closed")
    ]
    store.assert_claim(company, "status", "active", "2026-01-01", evidence[0], "test")
    store.assert_claim(company, "status", "closed", "2026-08-01", evidence[1], "test")

    cell = store.matrix("Company")["rows"][0]["cells"]["status"]
    assert cell["state"] == "Contested"
    assert set(cell["values"]) == {"active", "closed"}


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
