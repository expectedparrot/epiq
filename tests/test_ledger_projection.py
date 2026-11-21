import json
import random
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.ledger import LedgerRepository, Operation, Transaction
from epiq.ledger_projection import LedgerProjector


def project(tmp_path: Path) -> tuple[LedgerRepository, LedgerProjector]:
    repository = LedgerRepository.initialize(
        tmp_path / "project",
        "Projection test",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_projection",
        replica_id="rep_projection",
    )
    return repository, LedgerProjector(repository)


def append(
    repository: LedgerRepository,
    transaction_id: str,
    *,
    parents: tuple[str, ...] | None = None,
) -> Transaction:
    transaction = Transaction.create(
        repository.config.project_id,
        repository.config.replica_id,
        "agent:test",
        [Operation.create("entity_kind.define", {"kind": transaction_id})],
        transaction_id=transaction_id,
        recorded_at="2026-11-21T12:01:00Z",
        parents=repository.frontier() if parents is None else parents,
    )
    repository.append(transaction)
    return transaction


def projected_ids(projector: LedgerProjector) -> list[str]:
    with sqlite3.connect(projector.path) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT transaction_id FROM ledger_transactions ORDER BY ordinal"
            )
        ]


def semantic_snapshot(projector: LedgerProjector) -> dict[str, object]:
    """Capture projection meaning without depending on SQLite page layout."""
    with sqlite3.connect(projector.path) as connection:
        objects = connection.execute(
            """SELECT name,type FROM sqlite_master
               WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'
               ORDER BY name"""
        ).fetchall()
        snapshot: dict[str, object] = {}
        for name, object_type in objects:
            columns = tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{name}")')
            )
            rows = [
                tuple(row)
                for row in connection.execute(f'SELECT * FROM "{name}"')
            ]
            snapshot[str(name)] = {
                "type": str(object_type),
                "columns": columns,
                "rows": tuple(sorted(rows, key=repr)),
            }
        return snapshot


def append_concurrent_history(repository: LedgerRepository) -> None:
    genesis = repository.frontier()[0]
    left = append(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        parents=(genesis,),
    )
    right = append(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        parents=(genesis,),
    )
    append(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAZ",
        parents=(left.transaction_id, right.transaction_id),
    )


def test_full_rebuild_is_disposable_and_verifiable(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)

    built = projector.build()
    assert built["mode"] == "rebuild"
    assert built["transactions"] == 1
    assert projector.verify()["verified"] is True
    first_ids = projected_ids(projector)
    manifest = json.loads(projector.manifest_path.read_text())
    assert manifest["transaction_set_digest"] == repository.transaction_set_digest()

    projector.path.unlink()
    rebuilt = projector.build()
    assert rebuilt["mode"] == "rebuild"
    assert projected_ids(projector) == first_ids


def test_incremental_projection_and_current_noop(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    projector.build()
    item = append(repository, "txn_01ARZ3NDEKTSV4RRFFQ69G5FAV")

    incremental = projector.build()
    assert incremental["mode"] == "incremental"
    assert incremental["applied"] == 1
    assert projected_ids(projector)[-1] == item.transaction_id


def test_concurrent_projection_catch_up_serializes_ordinals(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    projector.build()
    appended = append(repository, "txn_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    workers = 12
    barrier = threading.Barrier(workers)

    def catch_up() -> dict[str, object]:
        barrier.wait()
        return LedgerProjector(repository).build()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(lambda _index: catch_up(), range(workers)))

    assert sum(int(item["applied"]) for item in results) == 1
    assert projected_ids(projector)[-1] == appended.transaction_id
    assert projector.verify()["verified"] is True
    assert projector.build()["mode"] == "current"


def test_git_merge_that_changes_order_forces_rebuild(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    genesis = repository.frontier()[0]
    later = append(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAY",
        parents=(genesis,),
    )
    projector.build()
    earlier = append(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        parents=(genesis,),
    )

    result = projector.build()
    assert result["mode"] == "rebuild"
    assert projected_ids(projector)[-2:] == [earlier.transaction_id, later.transaction_id]


def test_verify_detects_stale_or_corrupt_projection(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    projector.build()
    append(repository, "txn_01ARZ3NDEKTSV4RRFFQ69G5FAV")

    with pytest.raises(EpiqError) as stale:
        projector.verify()
    assert stale.value.code == "projection_out_of_date"

    projector.build()
    with sqlite3.connect(projector.path) as connection:
        connection.execute(
            "UPDATE projection_meta SET value='wrong' WHERE key='transaction_set_digest'"
        )
    with pytest.raises(EpiqError) as corrupt:
        projector.verify()
    assert corrupt.value.code == "projection_out_of_date"

    projector.build(force=True)
    with sqlite3.connect(projector.path) as connection:
        connection.execute("UPDATE ledger_operations SET payload_json='{}'")
    with pytest.raises(EpiqError) as content:
        projector.verify()
    assert content.value.code == "projection_content_mismatch"


def test_invalid_ledger_does_not_replace_good_projection(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    projector.build()
    before = projector.path.read_bytes()
    transaction = repository.transactions()[0]
    path = repository.root / transaction.relative_path()
    path.write_text(path.read_text().replace("project.initialize", "entity.create"))

    with pytest.raises(EpiqError) as error:
        projector.build(force=True)
    assert error.value.code == "ledger_digest_mismatch"
    assert projector.path.read_bytes() == before


def test_projection_is_independent_of_transaction_discovery_order(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    append_concurrent_history(repository)
    paths = repository.transaction_paths()
    expected_ids = [item.transaction_id for item in repository.ordered()]
    expected_snapshot: dict[str, object] | None = None
    expected_fingerprint: str | None = None

    for seed in range(12):
        shuffled = paths.copy()
        random.Random(seed).shuffle(shuffled)
        repository.transaction_paths = lambda shuffled=shuffled: shuffled  # type: ignore[method-assign]

        assert [item.transaction_id for item in repository.ordered()] == expected_ids
        projector.build(force=True)
        current = semantic_snapshot(projector)
        current_fingerprint = projector.semantic_fingerprint()
        if expected_snapshot is None:
            expected_snapshot = current
            expected_fingerprint = current_fingerprint
        else:
            assert current == expected_snapshot
            assert current_fingerprint == expected_fingerprint


def test_clean_checkout_rebuilds_identical_projection(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    append_concurrent_history(repository)
    projector.build(force=True)
    expected = semantic_snapshot(projector)

    checkout = tmp_path / "clean-checkout"
    shutil.copytree(
        repository.root,
        checkout,
        ignore=shutil.ignore_patterns(".epiq", ".git"),
    )
    clean_projector = LedgerProjector(LedgerRepository(checkout))

    assert not clean_projector.path.exists()
    result = clean_projector.build()
    assert result["mode"] == "rebuild"
    assert clean_projector.verify()["verified"] is True
    assert semantic_snapshot(clean_projector) == expected
    assert clean_projector.semantic_fingerprint() == projector.semantic_fingerprint()


def test_validation_and_build_parse_each_transaction_set_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, projector = project(tmp_path)
    append_concurrent_history(repository)
    original = repository.transactions
    calls = 0

    def counted() -> list[Transaction]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(repository, "transactions", counted)

    repository.validate()
    assert calls == 1
    calls = 0
    projector.build(force=True)
    assert calls == 1
    calls = 0
    projector.build()
    assert calls == 1
