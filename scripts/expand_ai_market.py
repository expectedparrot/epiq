"""Add the researched discovery cohort to an existing AI-interviewer workspace."""

from __future__ import annotations

import argparse
from typing import Any

from epiq.store import Store

CANDIDATES: list[dict[str, Any]] = [
    {
        "name": "Glaut",
        "domain": "glaut.com",
        "url": "https://www.glaut.com/ai-moderated-interviews",
        "title": "Glaut AI-moderated interviews",
        "excerpt": (
            "Glaut presents an AI-native research platform combining structured survey questions "
            "with AI-moderated voice and text interviews, dynamic probing, multilingual fieldwork, "
            "automated analysis, and respondent-quality controls."
        ),
        "positioning": "Survey-compatible AI-moderated voice and text research.",
        "features": {
            "feature_ai_moderation": "native",
            "feature_dynamic_probing": "native",
            "feature_multilingual": "native",
            "feature_voice": "native",
            "feature_text": "native",
            "feature_synthesis": "native",
        },
    },
    {
        "name": "Conveo",
        "domain": "conveo.ai",
        "url": "https://conveo.ai/product",
        "title": "Conveo product",
        "excerpt": (
            "Conveo presents an enterprise AI-led interview platform supporting video, voice, and "
            "text research, dynamic follow-up questions, multilingual interviews, automatic "
            "thematic analysis, and a persistent knowledge layer across research."
        ),
        "positioning": "Enterprise AI-led video interviews and persistent customer knowledge.",
        "features": {
            "feature_ai_moderation": "native",
            "feature_dynamic_probing": "native",
            "feature_multilingual": "native",
            "feature_video": "native",
            "feature_voice": "native",
            "feature_text": "native",
            "feature_synthesis": "native",
            "feature_repository": "native",
        },
        "funding": {
            "url": "https://conveo.ai/insights/conveo-raises-5-3m-to-revolutionize-market-research-with-ai-powered-video-interviews",
            "title": "Conveo USD 5.3 million seed announcement",
            "excerpt": "Conveo's company announcement reports a USD 5.3 million seed round.",
            "value": {"amount": 5300000, "currency": "USD", "round": "seed"},
            "as_of": "2025-03-06",
        },
    },
    {
        "name": "Koji Research",
        "domain": "koji.so",
        "url": "https://www.koji.so/",
        "title": "Koji Research product",
        "excerpt": (
            "Koji advertises adaptive AI interviews by voice, chat, and phone in more than 30 "
            "languages, with thematic analysis, traceable quotations, cross-study querying, REST "
            "API access, and MCP connectivity."
        ),
        "positioning": "Self-serve AI-native customer research by voice, chat, and phone.",
        "features": {
            "feature_ai_moderation": "native",
            "feature_dynamic_probing": "native",
            "feature_multilingual": "native",
            "feature_voice": "native",
            "feature_text": "native",
            "feature_synthesis": "native",
            "feature_repository": "native",
        },
        "pricing": {
            "url": "https://www.koji.so/pricing",
            "title": "Koji pricing",
            "excerpt": (
                "Koji publishes an entry plan starting at EUR 29 per month for 29 credits; text "
                "interviews use one credit and voice interviews use three credits."
            ),
            "value": {
                "model": "published_starting_price",
                "amount": 29,
                "currency": "EUR",
                "period": "month",
                "included": "29 credits",
            },
        },
    },
    {
        "name": "Tellet",
        "domain": "tellet.ai",
        "url": "https://tellet.ai/",
        "title": "Tellet product",
        "excerpt": (
            "Tellet presents an AI interview platform for simultaneous voice-rich qualitative "
            "interviews with dynamic follow-ups, voice, video and photo responses, multilingual "
            "delivery, transcription, analysis, and question-answering over transcripts."
        ),
        "positioning": "Voice-rich asynchronous qualitative interviews and automated analysis.",
        "features": {
            "feature_ai_moderation": "native",
            "feature_dynamic_probing": "native",
            "feature_multilingual": "native",
            "feature_video": "native",
            "feature_voice": "native",
            "feature_synthesis": "native",
        },
        "funding": {
            "url": "https://www.linkedin.com/posts/trytellet_tellet-closes-its-400000-pre-seed-investment-activity-7141305881031282688-qyC9",
            "title": "Tellet pre-seed announcement",
            "excerpt": "A Tellet company account announcement reports a EUR 400,000 pre-seed round.",
            "value": {"amount": 400000, "currency": "EUR", "round": "pre-seed"},
            "as_of": "2023-12-12",
            "confidence": "medium",
        },
    },
]


def add_evidence(store: Store, item: dict[str, Any], actor: str) -> str:
    """Create one evidence fragment and return its ID."""
    _, evidence_id = store.add_evidence(
        item["url"], item["title"], "2026-08-15", item["excerpt"], actor
    )
    return evidence_id


def expand(store: Store, actor: str) -> dict[str, Any]:
    """Add market taxonomy and the four discovery candidates."""
    store.add_question(
        "market_segment",
        "Company",
        "Enum[direct_specialist,platform_entrant,adjacent]",
        {"label": "Market segment", "cardinality": "one"},
        actor,
    )
    store.add_question(
        "comparison_status",
        "Company",
        "Enum[core,candidate]",
        {"label": "Comparison status", "cardinality": "one"},
        actor,
    )

    core_sources = {
        "Listen Labs": "https://listenlabs.ai/",
        "Outset": "https://outset.ai/",
        "Strella": "https://www.strella.io/product",
        "Yazi": "https://docs.askyazi.com/using-yazis-app/ai-moderated-interviews",
    }
    for company, url in core_sources.items():
        _, evidence_id = store.add_evidence(
            url,
            f"{company} product site",
            "2026-08-15",
            f"{company} publicly presents an AI-moderated customer-research product.",
            actor,
        )
        store.assert_claim(
            company, "market_segment", "direct_specialist", "2026-08-15", evidence_id, actor
        )
        store.assert_claim(company, "comparison_status", "core", "2026-08-15", evidence_id, actor)

    added: list[str] = []
    for candidate in CANDIDATES:
        entity_id = store.add_entity(
            "Company",
            candidate["name"],
            {
                "domain": candidate["domain"],
                "tags": ["ai-interviewer", "customer-research"],
                "discovery_cohort": "2026-08-15",
            },
            actor,
        )
        evidence_id = add_evidence(store, candidate, actor)
        store.assert_claim(
            entity_id,
            "positioning",
            {"text": candidate["positioning"]},
            "2026-08-15",
            evidence_id,
            actor,
        )
        store.assert_claim(
            entity_id, "market_segment", "direct_specialist", "2026-08-15", evidence_id, actor
        )
        store.assert_claim(
            entity_id, "comparison_status", "candidate", "2026-08-15", evidence_id, actor
        )
        for question, value in candidate["features"].items():
            store.assert_claim(entity_id, question, value, "2026-08-15", evidence_id, actor)
        if pricing := candidate.get("pricing"):
            pricing_evidence = add_evidence(store, pricing, actor)
            store.assert_claim(
                entity_id, "pricing", pricing["value"], "2026-08-15", pricing_evidence, actor
            )
        if funding := candidate.get("funding"):
            funding_evidence = add_evidence(store, funding, actor)
            store.assert_claim(
                entity_id,
                "funding_round",
                funding["value"],
                funding["as_of"],
                funding_evidence,
                actor,
                confidence=funding.get("confidence", "high"),
            )
        added.append(candidate["name"])
    return {"added": added, "core_classified": list(core_sources), "companies": 8}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--actor", default="agent:market-expansion")
    args = parser.parse_args()
    print(expand(Store(args.db), args.actor))


if __name__ == "__main__":
    main()
