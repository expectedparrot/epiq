from pathlib import Path

import pytest

import epiq
from epiq.errors import EpiqError
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


def test_public_api_can_initialize_a_missing_project(tmp_path: Path) -> None:
    path = tmp_path / "new-project.sqlite"

    database = epiq.open(path, create=True, name="New project")

    assert database.path == path
    assert Store(path).overview()["project"]["name"] == "New project"


def test_public_api_open_reports_typed_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(EpiqError) as missing_error:
        epiq.open(missing)
    assert missing_error.value.code == "uninitialized_database"
    assert not missing.exists()

    foreign = tmp_path / "foreign.sqlite"
    foreign.touch()
    with pytest.raises(EpiqError) as foreign_error:
        epiq.open(foreign)
    assert foreign_error.value.code == "uninitialized_database"

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(EpiqError) as corrupt_error:
        epiq.open(corrupt)
    assert corrupt_error.value.code == "invalid_database"
