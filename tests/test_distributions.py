from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.store import Store


@pytest.fixture
def weather(tmp_path: Path) -> tuple[Store, str]:
    store = Store(tmp_path / "weather.sqlite")
    store.initialize("Weather forecasts")
    event = store.add_entity("WeatherEvent", "Boston rain 2026-08-17", {}, "test")
    return store, event


@pytest.mark.parametrize("value", [0, 0.25, 1])
def test_probability_accepts_unit_interval(weather: tuple[Store, str], value: float) -> None:
    store, event = weather
    store.add_question("rain_probability", "WeatherEvent", "Probability", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/weather", "Forecast", "2026-08-16", f"Forecast {value}.", "test"
    )
    store.assert_claim(event, "rain_probability", value, "2026-08-17", evidence, "test")


@pytest.mark.parametrize("value", [-0.01, 1.01, True, "0.5"])
def test_probability_rejects_values_outside_unit_interval(
    weather: tuple[Store, str], value: object
) -> None:
    store, event = weather
    store.add_question("rain_probability", "WeatherEvent", "Probability", {}, "test")
    _, evidence = store.add_evidence(
        "https://example.test/weather", "Forecast", "2026-08-16", "Forecast.", "test"
    )
    with pytest.raises(EpiqError, match="Expected Probability"):
        store.assert_claim(event, "rain_probability", value, "2026-08-17", evidence, "test")


def test_categorical_distribution_validation(weather: tuple[Store, str]) -> None:
    store, event = weather
    store.add_question(
        "rain_outcome", "WeatherEvent", "Distribution[Enum[rain,no_rain]]", {}, "test"
    )
    _, evidence = store.add_evidence(
        "https://example.test/weather", "Forecast", "2026-08-16", "Rain 40%.", "test"
    )
    store.assert_claim(
        event,
        "rain_outcome",
        {"kind": "categorical", "probabilities": {"rain": 0.4, "no_rain": 0.6}},
        "2026-08-17",
        evidence,
        "test",
    )
    with pytest.raises(EpiqError, match="sum to 1"):
        store.assert_claim(
            event,
            "rain_outcome",
            {"kind": "categorical", "probabilities": {"rain": 0.4, "no_rain": 0.5}},
            "2026-08-17",
            evidence,
            "test",
        )


def test_derived_distribution_preserves_all_claim_and_evidence_lineage(
    weather: tuple[Store, str],
) -> None:
    store, event = weather
    store.add_question(
        "rain_probability",
        "WeatherEvent",
        "Probability",
        {"cardinality": "many"},
        "test",
    )
    store.add_question(
        "forecast_distribution",
        "WeatherEvent",
        "Distribution[Float]",
        {"cardinality": "one"},
        "test",
    )
    inputs = []
    samples = [0.4, 0.55, 0.35, 0.6, 0.45]
    for index, sample in enumerate(samples):
        _, evidence = store.add_evidence(
            f"https://example.test/forecast/{index}",
            f"Forecast {index}",
            "2026-08-16",
            f"Chance of rain: {sample}.",
            "test",
        )
        inputs.append(
            store.assert_claim(event, "rain_probability", sample, "2026-08-17", evidence, "test")
        )

    derived = store.derive_distribution(
        event, "forecast_distribution", inputs, "2026-08-17", "agent:ensemble"
    )
    cell = store.matrix("WeatherEvent")["rows"][0]["cells"]["forecast_distribution"]

    assert cell["value"] == {"kind": "empirical", "samples": samples}
    assert {item["evidence_id"] for item in cell["lineage"]} == {
        item["evidence_id"]
        for item in store.matrix("WeatherEvent")["rows"][0]["cells"]["rain_probability"]["lineage"]
    }
    assert len(cell["lineage"]) == 5
    assert cell["lineage"][0]["derivation"]["input_claim_ids"] == inputs
    assert cell["lineage"][0]["derivation"]["operation"] == "empirical"
    assert any(
        event["event_type"] == "claim.derive" and event["payload"]["claim_id"] == derived
        for event in store.history()
    )


def test_claim_can_cite_multiple_evidence_fragments(weather: tuple[Store, str]) -> None:
    store, event = weather
    store.add_question("summary", "WeatherEvent", "String", {}, "test")
    evidence = [
        store.add_evidence(
            f"https://example.test/{index}",
            f"Source {index}",
            "2026-08-16",
            f"Excerpt {index}",
            "test",
        )[1]
        for index in range(2)
    ]
    store.assert_claim(event, "summary", "Two sources agree.", "2026-08-17", evidence, "test")

    lineage = store.matrix("WeatherEvent")["rows"][0]["cells"]["summary"]["lineage"]
    assert [item["evidence_id"] for item in lineage] == evidence


@pytest.mark.parametrize(
    "value_type", ["ProbabilityDistribution", "Distribution[String]", "Enum[]"]
)
def test_unknown_or_malformed_question_types_are_rejected(
    weather: tuple[Store, str], value_type: str
) -> None:
    store, _ = weather
    with pytest.raises(EpiqError, match="Unknown or malformed"):
        store.add_question("invalid", "WeatherEvent", value_type, {}, "test")
