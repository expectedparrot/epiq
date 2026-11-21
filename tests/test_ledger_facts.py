import hashlib
import sqlite3
from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.ledger import LedgerRepository, Operation, Transaction
from epiq.ledger_projection import LedgerProjector


def setup(tmp_path: Path) -> tuple[LedgerRepository, LedgerProjector]:
    repository = LedgerRepository.initialize(
        tmp_path / "facts",
        "Facts",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_facts",
        replica_id="rep_facts",
    )
    transaction = Transaction.create(
        repository.config.project_id,
        repository.config.replica_id,
        "human:test",
        [
            Operation.create("entity_kind.define", {"kind": "Item"}),
            Operation.create(
                "entity.create",
                {"entity_id": "ent_item", "kind": "Item", "name": "Item"},
            ),
            Operation.create(
                "question.define",
                {
                    "question_id": "q_verdict",
                    "name": "verdict",
                    "subject_kind": "Item",
                    "value_type": "Enum[pass,fail]",
                },
            ),
        ],
        recorded_at="2026-11-21T12:01:00Z",
        parents=repository.frontier(),
    )
    repository.append(transaction)
    return repository, LedgerProjector(repository)


def commit(
    repository: LedgerRepository,
    transaction_id: str,
    operations: list[Operation],
    *,
    parents: tuple[str, ...] | None = None,
) -> Transaction:
    transaction = Transaction.create(
        repository.config.project_id,
        repository.config.replica_id,
        "agent:test",
        operations,
        transaction_id=transaction_id,
        recorded_at="2026-11-21T12:02:00Z",
        parents=repository.frontier() if parents is None else parents,
    )
    repository.append(transaction)
    return transaction


def capture(evidence_id: str, excerpt: str) -> Operation:
    return Operation.create(
        "evidence.capture",
        {
            "evidence_id": evidence_id,
            "source_id": f"src_{evidence_id}",
            "url": f"https://example.test/{evidence_id}",
            "source_type": "web",
            "title": evidence_id,
            "retrieved_at": "2026-11-21T07:00:00-05:00",
            "excerpt": excerpt,
            "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
        },
    )


def assertion(claim_id: str, evidence_id: str, value: str = "pass") -> Operation:
    return Operation.create(
        "claim.assert",
        {
            "claim_id": claim_id,
            "subject_id": "ent_item",
            "question_id": "q_verdict",
            "value": value,
            "valid_from": "2026-11-21",
            "evidence_ids": [evidence_id],
            "confidence": "high",
            "temporal_basis": "observed",
        },
    )


def rows(projector: LedgerProjector, query: str) -> list[tuple]:
    with sqlite3.connect(projector.path) as connection:
        return list(connection.execute(query))


def test_evidence_claim_support_and_retraction_are_add_only(tmp_path: Path) -> None:
    repository, projector = setup(tmp_path)
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [capture("evd_one", "pass"), assertion("clm_one", "evd_one")],
    )
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        [
            capture("evd_two", "additional support"),
            Operation.create(
                "claim.support",
                {
                    "claim_id": "clm_one",
                    "evidence_id": "evd_two",
                    "exact_quote": "additional support",
                },
            ),
            Operation.create(
                "claim.retract", {"claim_id": "clm_one", "reason": "incorrect"}
            ),
        ],
    )

    projector.build()
    assert rows(projector, "SELECT claim_id FROM projected_claims") == [("clm_one",)]
    assert rows(projector, "SELECT claim_id FROM projected_active_claims") == []
    assert rows(
        projector,
        "SELECT evidence_id FROM projected_claim_supports ORDER BY evidence_id",
    ) == [("evd_one",), ("evd_two",)]
    assert rows(
        projector, "SELECT closure_type,reason FROM projected_claim_closures"
    ) == [("retracted", "incorrect")]


def test_missing_evidence_rejects_rebuild_without_replacing_projection(tmp_path: Path) -> None:
    repository, projector = setup(tmp_path)
    projector.build()
    before = projector.path.read_bytes()
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [assertion("clm_one", "evd_missing")],
    )

    with pytest.raises(EpiqError) as missing:
        projector.build()
    assert missing.value.code == "missing_domain_object"
    assert projector.path.read_bytes() == before


def test_duplicate_capture_and_competing_reviews_are_explicit_conflicts(tmp_path: Path) -> None:
    repository, projector = setup(tmp_path)
    first = commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [capture("evd_one", "same"), assertion("clm_one", "evd_one")],
    )
    parents = (first.transaction_id,)
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        [
            capture("evd_two", "same"),
            Operation.create(
                "claim.review",
                {
                    "review_id": "rev_accept",
                    "claim_id": "clm_one",
                    "decision": "accepted",
                    "reason": "looks good",
                },
            ),
        ],
        parents=parents,
    )
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAY",
        [
            Operation.create(
                "claim.review",
                {
                    "review_id": "rev_reject",
                    "claim_id": "clm_one",
                    "decision": "rejected",
                    "reason": "not supported",
                },
            )
        ],
        parents=parents,
    )

    projector.build()
    assert rows(
        projector, "SELECT conflict_type FROM projected_conflicts ORDER BY conflict_type"
    ) == [("claim_review",), ("evidence_duplicate",)]


def test_supersession_references_exact_replacement_claim(tmp_path: Path) -> None:
    repository, projector = setup(tmp_path)
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [
            capture("evd_one", "pass"),
            assertion("clm_one", "evd_one"),
            capture("evd_two", "fail"),
            assertion("clm_two", "evd_two", "fail"),
            Operation.create(
                "claim.supersede",
                {
                    "claim_id": "clm_one",
                    "replacement_claim_id": "clm_two",
                    "reason": "corrected",
                },
            ),
        ],
    )

    projector.build()
    assert rows(
        projector,
        "SELECT claim_id,replacement_claim_id FROM projected_claim_closures",
    ) == [("clm_one", "clm_two")]
    assert rows(projector, "SELECT claim_id FROM projected_active_claims") == [("clm_two",)]
