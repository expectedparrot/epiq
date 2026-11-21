import hashlib
import threading
from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.ledger import LedgerRepository, Operation
from epiq.ledger_projection import LedgerProjector
from epiq.ledger_writer import LedgerWriter


def project(tmp_path: Path) -> tuple[LedgerRepository, LedgerWriter]:
    repository = LedgerRepository.initialize(
        tmp_path / "writer",
        "Writer",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_writer",
        replica_id="rep_writer",
    )
    return repository, LedgerWriter(repository)


def setup_operations() -> list[Operation]:
    return [
        Operation.create("entity_kind.define", {"kind": "Item"}),
        Operation.create(
            "entity.create", {"entity_id": "ent_item", "kind": "Item", "name": "Item"}
        ),
        Operation.create(
            "question.define",
            {
                "question_id": "q_verdict",
                "name": "verdict",
                "subject_kind": "Item",
                "value_type": "Bool",
            },
        ),
    ]


def test_writer_dry_runs_appends_and_projects(tmp_path: Path) -> None:
    repository, writer = project(tmp_path)

    result = writer.commit(
        setup_operations(),
        actor="agent:test",
        recorded_at="2026-11-21T12:01:00Z",
        idempotency_key="setup",
    )

    assert result["created"] is True
    assert result["projection"]["mode"] == "incremental"
    assert len(repository.transactions()) == 2
    assert writer.projector.verify()["verified"] is True


def test_invalid_write_never_reaches_ledger(tmp_path: Path) -> None:
    repository, writer = project(tmp_path)
    before = repository.transaction_set_digest()
    operation = Operation.create(
        "claim.assert",
        {
            "claim_id": "clm_bad",
            "subject_id": "ent_missing",
            "question_id": "q_missing",
            "value": True,
            "valid_from": "2026-11-21",
            "evidence_ids": ["evd_missing"],
            "confidence": "high",
            "temporal_basis": "observed",
        },
    )

    with pytest.raises(EpiqError) as error:
        writer.commit(
            [operation], actor="agent:test", recorded_at="2026-11-21T12:01:00Z"
        )
    assert error.value.code == "missing_domain_object"
    assert repository.transaction_set_digest() == before


def test_idempotent_retry_recovers_projection_after_post_append_crash(tmp_path: Path) -> None:
    repository, writer = project(tmp_path)
    operations = setup_operations()

    def crash(_transaction):
        raise RuntimeError("simulated process exit")

    with pytest.raises(RuntimeError, match="simulated"):
        writer.commit(
            operations,
            actor="agent:test",
            recorded_at="2026-11-21T12:01:00Z",
            idempotency_key="setup",
            after_append=crash,
        )
    assert len(repository.transactions()) == 2
    with pytest.raises(EpiqError) as stale:
        LedgerProjector(repository).verify()
    assert stale.value.code == "projection_out_of_date"

    retried = LedgerWriter(repository).commit(
        operations,
        actor="agent:test",
        recorded_at="2026-11-21T13:00:00Z",
        idempotency_key="setup",
    )
    assert retried["created"] is False
    assert len(repository.transactions()) == 2
    assert LedgerProjector(repository).verify()["verified"] is True


def test_idempotency_key_rejects_different_request(tmp_path: Path) -> None:
    _repository, writer = project(tmp_path)
    writer.commit(
        setup_operations(),
        actor="agent:test",
        recorded_at="2026-11-21T12:01:00Z",
        idempotency_key="same-key",
    )

    with pytest.raises(EpiqError) as conflict:
        writer.commit(
            [Operation.create("entity_kind.define", {"kind": "Other"})],
            actor="agent:test",
            recorded_at="2026-11-21T12:02:00Z",
            idempotency_key="same-key",
        )
    assert conflict.value.code == "idempotency_conflict"


def test_concurrent_same_request_creates_one_transaction(tmp_path: Path) -> None:
    repository, _writer = project(tmp_path)
    operations = setup_operations()
    results: list[dict] = []

    def write() -> None:
        results.append(
            LedgerWriter(repository).commit(
                operations,
                actor="agent:test",
                recorded_at="2026-11-21T12:01:00Z",
                idempotency_key="concurrent",
            )
        )

    threads = [threading.Thread(target=write) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(result["created"] for result in results) == [False, True]
    assert len(repository.transactions()) == 2


def test_evidence_and_claim_commit_is_atomic(tmp_path: Path) -> None:
    repository, writer = project(tmp_path)
    writer.commit(
        setup_operations(), actor="agent:test", recorded_at="2026-11-21T12:01:00Z"
    )
    excerpt = "The item passed."
    operations = [
        Operation.create(
            "evidence.capture",
            {
                "evidence_id": "evd_result",
                "source_id": "src_result",
                "url": "https://example.test/result",
                "source_type": "web",
                "title": "Result",
                "retrieved_at": "2026-11-21T12:02:00Z",
                "excerpt": excerpt,
                "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
            },
        ),
        Operation.create(
            "claim.assert",
            {
                "claim_id": "clm_result",
                "subject_id": "ent_item",
                "question_id": "q_verdict",
                "value": True,
                "valid_from": "2026-11-21",
                "evidence_ids": ["evd_result"],
                "confidence": "high",
                "temporal_basis": "observed",
            },
        ),
    ]

    result = writer.commit(
        operations,
        actor="agent:test",
        recorded_at="2026-11-21T12:02:00Z",
        idempotency_key="result",
    )
    assert result["created"] is True
    assert LedgerProjector(repository).verify()["verified"] is True
