"""Import adapters for research corpora produced before Epiq."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import Store


def _read(path: str | Path) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text())
    if not isinstance(value, list):
        raise ValueError(f"Expected a JSON array: {path}")
    return value


def import_cham_corpus(
    store: Store,
    entities_path: str | Path,
    evidence_path: str | Path,
    claims_path: str | Path,
    actor: str = "import:cham",
) -> dict[str, Any]:
    """Translate the original Cham JSON packet into typed Epiq questions."""
    entities = _read(entities_path)
    evidence = _read(evidence_path)
    claims = _read(claims_path)
    entity_ids: dict[str, str] = {}
    feature_questions: dict[str, str] = {}

    for item in entities:
        kind = str(item["kind"]).title()
        entity_ids[str(item["ref"])] = store.add_entity(
            kind,
            str(item["canonical_name"]),
            {
                "original_ref": item["ref"],
                "domain": item.get("domain"),
                "aliases": item.get("aliases", []),
                "tags": item.get("tags", []),
            },
            actor,
        )

    base_questions = {
        "positioning": store.add_question(
            "positioning",
            "Company",
            "Json",
            {"label": "Positioning", "cardinality": "one", "fresh_days": 120},
            actor,
        ),
        "pricing": store.add_question(
            "pricing",
            "Company",
            "Json",
            {"label": "Public pricing", "cardinality": "one", "fresh_days": 60},
            actor,
        ),
        "funding_round": store.add_question(
            "funding_round",
            "Company",
            "Json",
            {"label": "Funding rounds", "cardinality": "many"},
            actor,
        ),
    }
    for feature in (item for item in entities if item["kind"] == "feature"):
        name = "feature_" + str(feature["ref"]).split(".", 1)[1]
        feature_questions[str(feature["ref"])] = store.add_question(
            name,
            "Company",
            "Enum[native,integration,beta,announced]",
            {
                "label": feature["canonical_name"],
                "cardinality": "one",
                "fresh_days": 90,
                "controlled_feature_ref": feature["ref"],
            },
            actor,
        )

    evidence_ids: dict[str, str] = {}
    for item in evidence:
        _, evidence_id = store.add_evidence(
            str(item["url"]),
            str(item.get("title") or item["url"]),
            str(item["retrieved_at"]),
            str(item["excerpt"]),
            actor,
        )
        evidence_ids[str(item["ref"])] = evidence_id

    imported_claims = 0
    multi_evidence_claims = 0
    for item in claims:
        predicate = str(item["predicate"])
        value = item["value"]
        if predicate == "has_feature":
            feature_ref = str(value["feature_id"])
            question_id = feature_questions[feature_ref]
            claim_value = value["availability"]
        else:
            question_id = base_questions[predicate]
            claim_value = value
        cited = list(item["evidence"])
        if len(cited) > 1:
            multi_evidence_claims += 1
        store.assert_claim(
            entity_ids[str(item["subject"])],
            question_id,
            claim_value,
            str(item["as_of"]),
            evidence_ids[str(cited[0])],
            actor,
            confidence=str(item.get("confidence", "medium")),
        )
        imported_claims += 1

    return {
        "entities": len(entity_ids),
        "companies": sum(item["kind"] == "company" for item in entities),
        "features": len(feature_questions),
        "questions": len(base_questions) + len(feature_questions),
        "evidence": len(evidence_ids),
        "claims": imported_claims,
        "multi_evidence_claims_using_primary_only": multi_evidence_claims,
    }
