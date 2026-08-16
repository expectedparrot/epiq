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
