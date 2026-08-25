from pathlib import Path

import epiq
from epiq.store import Store


def test_public_embedding_api_batches_and_reads_long_claims(tmp_path: Path) -> None:
    path = tmp_path / "embedded.sqlite"
    store = Store(path)
    store.initialize("Embedded")
    store.add_entity("Item", "Example", {}, "setup")
    store.add_question("verdict", "Item", "Bool", {}, "setup")

    database = epiq.open(path)
    results = database.batch_write(
        [
            {
                "op": "evidence.add",
                "ref": "result",
                "url": "urn:test:result",
                "source_type": "model",
                "title": "Result",
                "retrieved_at": "2026-08-25",
                "excerpt": "pass",
            },
            {
                "op": "claim.assert",
                "subject": "Example",
                "question": "verdict",
                "value": True,
                "valid_from": "2026-08-25",
                "evidence_refs": ["result"],
            },
        ],
        actor="model:test",
    )

    assert len(results) == 2
    assert database.claims(kind="Item")[0]["actor"] == "model:test"
