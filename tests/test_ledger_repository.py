from dataclasses import replace
from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.ledger import LedgerRepository, Operation, Transaction, document_digest


def initialized(tmp_path: Path) -> LedgerRepository:
    return LedgerRepository.initialize(
        tmp_path / "project",
        "Test project",
        actor="human:test",
        recorded_at="2026-11-21T10:00:00Z",
        project_id="prj_test",
        replica_id="rep_test",
    )


def child(
    repository: LedgerRepository,
    transaction_id: str,
    *,
    parents: tuple[str, ...] | None = None,
) -> Transaction:
    return Transaction.create(
        repository.config.project_id,
        repository.config.replica_id,
        "agent:test",
        [Operation.create("entity_kind.define", {"kind": transaction_id})],
        transaction_id=transaction_id,
        recorded_at="2026-11-21T10:01:00Z",
        parents=parents if parents is not None else repository.frontier(),
    )


def test_initialize_creates_git_project_and_genesis(tmp_path: Path) -> None:
    repository = initialized(tmp_path)

    assert repository.config.name == "Test project"
    assert (repository.root / "epiq.toml").is_file()
    assert (repository.root / "ledger/schemas/ledger-transaction-1.schema.json").is_file()
    assert (repository.root / ".gitignore").read_text() == ".epiq/\n"
    report = repository.validate()
    assert report["transactions"] == 1
    assert report["operations"] == 1
    assert repository.ordered()[0].operations[0].type == "project.initialize"


def test_append_is_atomic_idempotent_and_causally_ordered(tmp_path: Path) -> None:
    repository = initialized(tmp_path)
    genesis = repository.frontier()[0]
    later = child(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        parents=("txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",),
    )
    first = child(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        parents=(genesis,),
    )
    concurrent = child(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAY",
        parents=(genesis,),
    )

    assert repository.append(first) is True
    assert repository.append(first) is False
    assert repository.append(concurrent) is True
    assert repository.append(later) is True
    assert [item.transaction_id for item in repository.ordered()][1:] == [
        first.transaction_id,
        later.transaction_id,
        concurrent.transaction_id,
    ]
    assert repository.frontier() == (later.transaction_id, concurrent.transaction_id)


def test_append_rejects_missing_parent_project_mismatch_and_collision(tmp_path: Path) -> None:
    repository = initialized(tmp_path)
    missing = child(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        parents=("txn_01ARZ3NDEKTSV4RRFFQ69G5FAZ",),
    )
    with pytest.raises(EpiqError) as missing_error:
        repository.append(missing)
    assert missing_error.value.code == "missing_parent"

    mismatch = replace(missing, project_id="prj_other")
    with pytest.raises(EpiqError) as project_error:
        repository.append(mismatch)
    assert project_error.value.code == "ledger_project_mismatch"

    valid = child(repository, "txn_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    repository.append(valid)
    changed = Transaction.create(
        repository.config.project_id,
        repository.config.replica_id,
        "agent:test",
        [Operation.create("entity_kind.define", {"kind": "changed"})],
        transaction_id=valid.transaction_id,
        recorded_at=valid.recorded_at,
        parents=valid.parents,
    )
    with pytest.raises(EpiqError) as collision:
        repository.append(changed)
    assert collision.value.code == "transaction_id_collision"


def test_validation_detects_tampering_wrong_paths_and_cycles(tmp_path: Path) -> None:
    repository = initialized(tmp_path)
    item = child(repository, "txn_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    repository.append(item)
    path = repository.root / item.relative_path()
    path.write_text(path.read_text().replace("entity_kind.define", "entity.retire"))
    with pytest.raises(EpiqError) as tampered:
        repository.validate()
    assert tampered.value.code == "ledger_digest_mismatch"

    path.write_bytes(item.canonical())
    wrong = path.parent.parent / "ZZ" / path.name
    wrong.parent.mkdir()
    path.replace(wrong)
    with pytest.raises(EpiqError) as misplaced:
        repository.validate()
    assert misplaced.value.code == "invalid_transaction_path"


def test_validation_detects_cycle_merged_from_other_branches(tmp_path: Path) -> None:
    repository = initialized(tmp_path)
    identifiers = (
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAX",
    )
    transactions = [
        child(repository, identifiers[0], parents=(identifiers[1],)),
        child(repository, identifiers[1], parents=(identifiers[0],)),
    ]
    for item in transactions:
        document = item.as_dict()
        document["digest"] = document_digest(document)
        rewritten = Transaction.from_dict(document)
        path = repository.root / rewritten.relative_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rewritten.canonical())

    with pytest.raises(EpiqError) as cycle:
        repository.ordered()
    assert cycle.value.code == "causal_cycle"
