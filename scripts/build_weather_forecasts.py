"""Build an illustrative five-provider rain-forecast ensemble."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from epiq.store import Store

FORECASTS = [
    ("NOAA", 0.40),
    ("Weather.com", 0.55),
    ("AccuWeather", 0.35),
    ("Apple Weather", 0.60),
    ("Local station", 0.45),
]


def build(path: str | Path, actor: str = "agent:weather-example") -> dict[str, Any]:
    """Create atomic provider forecasts and an equally weighted empirical ensemble."""
    store = Store(path)
    store.initialize("Illustrative Boston Rain Forecasts")
    event = store.add_entity(
        "WeatherEvent",
        "Boston rain on 2026-08-17",
        {"location": "Boston, MA", "target_date": "2026-08-17"},
        actor,
    )
    store.add_question(
        "rain_probability",
        "WeatherEvent",
        "Probability",
        {"label": "Provider rain probabilities", "cardinality": "many"},
        actor,
    )
    store.add_question(
        "forecast_distribution",
        "WeatherEvent",
        "Distribution[Float]",
        {"label": "Forecast ensemble", "cardinality": "one"},
        actor,
    )

    input_claims = []
    for provider, probability in FORECASTS:
        slug = provider.lower().replace(" ", "-").replace(".", "")
        _, evidence = store.add_evidence(
            f"https://example.test/weather/{slug}/2026-08-17",
            f"{provider} illustrative forecast",
            "2026-08-16",
            f"{provider} assigns a {probability:.0%} chance of rain in Boston on August 17, 2026.",
            actor,
        )
        input_claims.append(
            store.assert_claim(
                event,
                "rain_probability",
                probability,
                "2026-08-17",
                evidence,
                actor,
            )
        )

    distribution_claim = store.derive_distribution(
        event,
        "forecast_distribution",
        input_claims,
        "2026-08-17",
        actor,
        confidence="medium",
    )
    return {
        "event_id": event,
        "forecasts": len(FORECASTS),
        "input_claim_ids": input_claims,
        "distribution_claim_id": distribution_claim,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--actor", default="agent:weather-example")
    args = parser.parse_args()
    print(build(args.db, args.actor))


if __name__ == "__main__":
    main()
