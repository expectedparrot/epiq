import hashlib
from pathlib import Path

from epiq.ledger import LedgerRepository, Operation
from epiq.ledger_view import LedgerView
from epiq.ledger_writer import LedgerWriter


def populated(tmp_path: Path) -> tuple[LedgerRepository, LedgerWriter, LedgerView]:
    repository = LedgerRepository.initialize(
        tmp_path / "view",
        "View",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_view",
        replica_id="rep_view",
    )
    writer = LedgerWriter(repository)
    excerpt = "one"
    writer.commit(
        [
            Operation.create("entity_kind.define", {"kind": "Item"}),
            Operation.create(
                "entity.create",
                {
                    "entity_id": "ent_one",
                    "kind": "Item",
                    "name": "One",
                    "attributes": {"group": "a"},
                },
            ),
            Operation.create(
                "question.define",
                {
                    "question_id": "q_value",
                    "name": "value",
                    "subject_kind": "Item",
                    "value_type": "Int",
                },
            ),
            Operation.create(
                "evidence.capture",
                {
                    "evidence_id": "evd_one",
                    "source_id": "src_one",
                    "url": "https://example.test",
                    "source_type": "web",
                    "title": "One",
                    "retrieved_at": "2026-11-21",
                    "excerpt": excerpt,
                    "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
                },
            ),
            Operation.create(
                "claim.assert",
                {
                    "claim_id": "clm_one",
                    "subject_id": "ent_one",
                    "question_id": "q_value",
                    "value": 1,
                    "valid_from": "2026-11-21",
                    "evidence_ids": ["evd_one"],
                    "confidence": "high",
                    "temporal_basis": "observed",
                },
            ),
        ],
        actor="agent:test",
        recorded_at="2026-11-21T12:01:00Z",
    )
    return repository, writer, LedgerView(repository)


def test_view_returns_json_ready_records_and_matrix(tmp_path: Path) -> None:
    _, _, view = populated(tmp_path)

    assert view.entities(kind="Item")[0]["attributes"] == {"group": "a"}
    assert view.questions(kind="Item")[0]["question_id"] == "q_value"
    assert view.evidence(evidence_ids=["evd_one"])[0]["excerpt"] == "one"
    claim = view.claims(kind="Item", question="value")[0]
    assert claim["value"] == 1
    assert claim["evidence_ids"] == ["evd_one"]
    assert view.matrix("Item")["cells"]["ent_one"]["q_value"] == [claim]


def test_matrix_preserves_multiple_active_claims_in_one_cell(tmp_path: Path) -> None:
    _, writer, view = populated(tmp_path)
    writer.commit(
        [
            Operation.create(
                "claim.assert",
                {
                    "claim_id": "clm_two",
                    "subject_id": "ent_one",
                    "question_id": "q_value",
                    "value": 2,
                    "valid_from": "2026-11-21",
                    "evidence_ids": ["evd_one"],
                    "confidence": "medium",
                    "temporal_basis": "observed",
                },
            )
        ],
        actor="agent:other",
        recorded_at="2026-11-21T12:02:00Z",
    )

    cell = view.matrix("Item")["cells"]["ent_one"]["q_value"]
    assert [item["claim_id"] for item in cell] == ["clm_one", "clm_two"]


def test_closed_claims_and_conflicts_are_explicit(tmp_path: Path) -> None:
    _, writer, view = populated(tmp_path)
    writer.commit(
        [Operation.create("claim.retract", {"claim_id": "clm_one", "reason": "bad"})],
        actor="human:test",
        recorded_at="2026-11-21T12:02:00Z",
    )

    assert view.claims() == []
    assert [item["claim_id"] for item in view.claims(include_closed=True)] == ["clm_one"]

    writer.commit(
        [
            Operation.create(
                "claim.supersede",
                {
                    "claim_id": "clm_one",
                    "replacement_claim_id": "clm_one",
                    "reason": "competing decision",
                },
            )
        ],
        actor="human:other",
        recorded_at="2026-11-21T12:03:00Z",
    )
    assert view.conflicts(conflict_type="claim_closure")[0]["object_ids"] == ["clm_one"]
