from pathlib import Path

import pytest

import epiq
from epiq.errors import EpiqError


def clock() -> str:
    return "2026-11-21T12:00:00Z"


def test_open_create_transact_and_read(tmp_path: Path) -> None:
    database = epiq.open_ledger(
        tmp_path / "project",
        create=True,
        name="Project",
        project_id="prj_api",
        replica_id="rep_api",
        clock=clock,
    )
    result = database.transact(
        [
            {"type": "entity_kind.define", "payload": {"kind": "Item"}},
            {
                "type": "entity.create",
                "payload": {"entity_id": "ent_one", "kind": "Item", "name": "One"},
            },
        ],
        idempotency_key="create-one",
    )

    assert result["created"] is True
    assert database.entities() == [
        {
            "entity_id": "ent_one",
            "kind": "Item",
            "name": "One",
            "attributes": {},
            "role": "entity",
            "identity": None,
            "visible": True,
        }
    ]
    assert database.status()["state"] == "ready"

    reopened = epiq.open_ledger(tmp_path / "project", clock=clock)
    assert reopened.entities() == database.entities()


def test_transact_idempotency_and_validation(tmp_path: Path) -> None:
    database = epiq.open_ledger(
        tmp_path / "project",
        create=True,
        project_id="prj_api",
        replica_id="rep_api",
        clock=clock,
    )
    operation = {"type": "entity_kind.define", "payload": {"kind": "Item"}}
    first = database.transact([operation], idempotency_key="same")
    second = database.transact([operation], idempotency_key="same")
    assert first["transaction_id"] == second["transaction_id"]
    assert second["created"] is False

    with pytest.raises(EpiqError, match="another request"):
        database.transact(
            [{"type": "entity_kind.define", "payload": {"kind": "Other"}}],
            idempotency_key="same",
        )


def test_existing_non_project_directory_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "not-a-project"
    path.mkdir()
    with pytest.raises(EpiqError):
        epiq.open_ledger(path)
