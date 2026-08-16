import json
from pathlib import Path

from epiq.importers import import_cham_corpus
from epiq.store import Store


def test_cham_corpus_becomes_typed_matrix(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    fixtures = {
        "entities.json": [
            {
                "ref": "company.example",
                "kind": "company",
                "canonical_name": "Example Research",
                "domain": "example.test",
            },
            {
                "ref": "feature.voice",
                "kind": "feature",
                "canonical_name": "Voice interviews",
            },
        ],
        "evidence.json": [
            {
                "ref": "evidence.product",
                "url": "https://example.test/product",
                "title": "Product page",
                "retrieved_at": "2026-01-01",
                "excerpt": "Example supports native voice interviews.",
            }
        ],
        "claims.json": [
            {
                "subject": "company.example",
                "predicate": "has_feature",
                "value": {"feature_id": "feature.voice", "availability": "native"},
                "confidence": "high",
                "as_of": "2026-01-01",
                "evidence": ["evidence.product"],
            }
        ],
    }
    for filename, value in fixtures.items():
        (corpus / filename).write_text(json.dumps(value))
    store = Store(tmp_path / "market.sqlite")
    store.initialize("AI interviewers")
    result = import_cham_corpus(
        store,
        corpus / "entities.json",
        corpus / "evidence.json",
        corpus / "claims.json",
    )
    assert result["companies"] == 1
    assert result["claims"] == 1

    matrix = store.matrix(
        "Company",
        ["feature_voice", "pricing"],
    )
    by_name = {row["name"]: row["cells"] for row in matrix["rows"]}
    assert by_name["Example Research"]["feature_voice"]["value"] == "native"
    assert by_name["Example Research"]["pricing"]["state"] == "Unasked"
