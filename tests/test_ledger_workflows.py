import hashlib
import sqlite3
from pathlib import Path

from epiq.ledger import LedgerRepository, Operation
from epiq.ledger_projection import LedgerProjector
from epiq.ledger_writer import LedgerWriter


def project(tmp_path: Path) -> tuple[LedgerRepository, LedgerWriter]:
    repository = LedgerRepository.initialize(
        tmp_path / "workflows",
        "Workflows",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_workflows",
        replica_id="rep_workflows",
    )
    writer = LedgerWriter(repository)
    excerpt = "value 1"
    writer.commit(
        [
            Operation.create("entity_kind.define", {"kind": "Item"}),
            Operation.create(
                "entity.create", {"entity_id": "ent_item", "kind": "Item", "name": "Item"}
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
                    "url": "https://example.test/one",
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
                    "claim_id": "clm_input",
                    "subject_id": "ent_item",
                    "question_id": "q_value",
                    "value": 0,
                    "valid_from": "2026-11-20",
                    "evidence_ids": ["evd_one"],
                    "confidence": "high",
                    "temporal_basis": "observed",
                },
            ),
            Operation.create(
                "claim.assert",
                {
                    "claim_id": "clm_one",
                    "subject_id": "ent_item",
                    "question_id": "q_value",
                    "value": 1,
                    "valid_from": "2026-11-21",
                    "evidence_ids": ["evd_one"],
                    "confidence": "high",
                    "temporal_basis": "observed",
                },
            ),
        ],
        actor="human:test",
        recorded_at="2026-11-21T12:01:00Z",
    )
    return repository, writer


def rows(repository: LedgerRepository, query: str) -> list[tuple]:
    projector = LedgerProjector(repository)
    projector.build()
    with sqlite3.connect(projector.path) as connection:
        return list(connection.execute(query))


def test_derivation_challenge_research_bundle_and_report_project(tmp_path: Path) -> None:
    repository, writer = project(tmp_path)
    input_transaction = repository.ordered()[-1].transaction_id
    content_digest = "sha256:" + hashlib.sha256(b"report").hexdigest()
    writer.commit(
        [
            Operation.create(
                "question.challenge",
                {
                    "challenge_id": "chg_value",
                    "question_id": "q_value",
                    "problem": "definition_ambiguity",
                    "explanation": "Clarify the unit",
                    "evidence_ids": ["evd_one"],
                },
            ),
            Operation.create(
                "question.challenge_resolve",
                {
                    "resolution_id": "res_value",
                    "challenge_id": "chg_value",
                    "status": "resolved",
                    "resolution": "Unit is count",
                },
            ),
            Operation.create(
                "derivation.declare",
                {
                    "derivation_id": "drv_one",
                    "claim_id": "clm_one",
                    "operation": "copy",
                    "input_claim_ids": ["clm_input"],
                    "parameter_claim_ids": [],
                    "path_claim_ids": [],
                    "parameters": {},
                },
            ),
            Operation.create(
                "research.not_found",
                {
                    "outcome_id": "out_one",
                    "subject_id": "ent_item",
                    "question_id": "q_value",
                    "query": "item value",
                    "notes": "No newer source",
                },
            ),
            Operation.create(
                "source.bundle_record",
                {
                    "bundle_id": "bnd_one",
                    "job_id": "job_one",
                    "request": {"query": "item value"},
                    "bundle": {"documents": [], "attempts": []},
                },
            ),
            Operation.create(
                "report.record",
                {
                    "report_id": "rpt_one",
                    "report_type": "summary",
                    "parameters": {},
                    "input_transaction_ids": [input_transaction],
                    "content_digest": content_digest,
                },
            ),
        ],
        actor="agent:test",
        recorded_at="2026-11-21T12:02:00Z",
    )

    assert rows(repository, "SELECT challenge_id FROM projected_question_challenges") == [
        ("chg_value",)
    ]
    assert rows(repository, "SELECT bundle_id FROM projected_source_bundles") == [("bnd_one",)]
    assert rows(repository, "SELECT report_id FROM projected_reports") == [("rpt_one",)]


def test_proposals_and_competing_reviews_are_preserved(tmp_path: Path) -> None:
    repository, writer = project(tmp_path)
    digest = "sha256:" + hashlib.sha256(b"proposal").hexdigest()
    proposed = writer.commit(
        [
            Operation.create(
                "proposal.create",
                {
                    "proposal_id": "prp_one",
                    "proposal_type": "document",
                    "document": {"operations": []},
                    "request_digest": digest,
                },
            )
        ],
        actor="agent:test",
        recorded_at="2026-11-21T12:02:00Z",
    )["transaction"]
    parents = [proposed.transaction_id]
    for transaction_id, review_id, decision in (
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAW", "rev_yes", "accepted"),
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAX", "rev_no", "rejected"),
    ):
        writer.commit(
            [
                Operation.create(
                    "proposal.review",
                    {
                        "review_id": review_id,
                        "proposal_id": "prp_one",
                        "decision": decision,
                        "reason": decision,
                    },
                )
            ],
            actor="human:test",
            recorded_at="2026-11-21T12:03:00Z",
            parents=parents,
            transaction_id=transaction_id,
        )

    assert rows(
        repository,
        "SELECT conflict_type FROM projected_conflicts WHERE conflict_type='proposal_review'",
    ) == [("proposal_review",)]
