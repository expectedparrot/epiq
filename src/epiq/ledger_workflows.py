"""Reducers for durable review, derivation, research, and report workflows."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .errors import EpiqError
from .ledger_domain import _conflict, _json, _require_row

WORKFLOW_SCHEMA = """
CREATE TABLE projected_question_challenges (
    challenge_id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES projected_questions(question_id),
    problem TEXT NOT NULL,
    explanation TEXT NOT NULL,
    example_entity_id TEXT REFERENCES projected_entities(entity_id),
    evidence_ids_json TEXT NOT NULL,
    proposed_replacement_json TEXT,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_question_challenge_resolutions (
    resolution_id TEXT PRIMARY KEY,
    challenge_id TEXT NOT NULL REFERENCES projected_question_challenges(challenge_id),
    status TEXT NOT NULL CHECK(status IN ('resolved','dismissed')),
    resolution TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_derivations (
    derivation_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES projected_claims(claim_id),
    operation TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_derivation_inputs (
    derivation_id TEXT NOT NULL REFERENCES projected_derivations(derivation_id),
    input_claim_id TEXT NOT NULL REFERENCES projected_claims(claim_id),
    role TEXT NOT NULL CHECK(role IN ('operand','parameter','path')),
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(derivation_id,role,ordinal),
    UNIQUE(derivation_id,role,input_claim_id)
);

CREATE TABLE projected_proposals (
    proposal_id TEXT PRIMARY KEY,
    proposal_type TEXT NOT NULL,
    document_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_proposal_reviews (
    review_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES projected_proposals(proposal_id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    reason TEXT NOT NULL,
    accepted_operation_ids_json TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_research_outcomes (
    outcome_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES projected_entities(entity_id),
    question_id TEXT NOT NULL REFERENCES projected_questions(question_id),
    query TEXT NOT NULL,
    notes TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_source_bundles (
    bundle_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_reports (
    report_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    input_transaction_ids_json TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);
"""

_SPECS: dict[str, tuple[set[str], set[str]]] = {
    "question.challenge": (
        {"challenge_id", "question_id", "problem", "explanation"},
        {"example_entity_id", "evidence_ids", "proposed_replacement"},
    ),
    "question.challenge_resolve": (
        {"resolution_id", "challenge_id", "status", "resolution"},
        set(),
    ),
    "derivation.declare": (
        {
            "derivation_id",
            "claim_id",
            "operation",
            "input_claim_ids",
            "parameter_claim_ids",
            "path_claim_ids",
            "parameters",
        },
        set(),
    ),
    "proposal.create": (
        {"proposal_id", "proposal_type", "document", "request_digest"},
        set(),
    ),
    "proposal.review": (
        {"review_id", "proposal_id", "decision", "reason"},
        {"accepted_operation_ids"},
    ),
    "research.not_found": (
        {"outcome_id", "subject_id", "question_id", "query", "notes"},
        set(),
    ),
    "source.bundle_record": ({"bundle_id", "job_id", "request", "bundle"}, set()),
    "report.record": (
        {"report_id", "report_type", "parameters", "input_transaction_ids", "content_digest"},
        set(),
    ),
}


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EpiqError("invalid_operation_payload", f"{key} must be a non-empty string")
    return value


def _string_list(payload: dict[str, Any], key: str, *, allow_empty: bool = True) -> list[str]:
    value = payload.get(key)
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise EpiqError("invalid_operation_payload", f"{key} must contain unique strings")
    return value


def validate_payload(operation_type: str, payload: dict[str, Any]) -> None:
    if operation_type not in _SPECS:
        return
    required, optional = _SPECS[operation_type]
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing or unknown:
        raise EpiqError(
            "invalid_operation_payload",
            f"{operation_type} missing={sorted(missing)} unknown={sorted(unknown)}",
        )
    non_strings = {
        "evidence_ids",
        "proposed_replacement",
        "input_claim_ids",
        "parameter_claim_ids",
        "path_claim_ids",
        "parameters",
        "document",
        "accepted_operation_ids",
        "request",
        "bundle",
        "input_transaction_ids",
    }
    for key in required - non_strings:
        _string(payload, key)
    if operation_type == "question.challenge":
        _string_list({"evidence_ids": payload.get("evidence_ids", [])}, "evidence_ids")
        if payload.get("proposed_replacement") is not None and not isinstance(
            payload["proposed_replacement"], dict
        ):
            raise EpiqError("invalid_operation_payload", "proposed_replacement must be an object")
    elif operation_type == "question.challenge_resolve":
        if payload["status"] not in {"resolved", "dismissed"}:
            raise EpiqError("invalid_operation_payload", "Unknown challenge resolution status")
    elif operation_type == "derivation.declare":
        _string_list(payload, "input_claim_ids", allow_empty=False)
        _string_list(payload, "parameter_claim_ids")
        _string_list(payload, "path_claim_ids")
        if not isinstance(payload["parameters"], dict):
            raise EpiqError("invalid_operation_payload", "parameters must be an object")
    elif operation_type == "proposal.create":
        if not isinstance(payload["document"], dict):
            raise EpiqError("invalid_operation_payload", "proposal document must be an object")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", payload["request_digest"]):
            raise EpiqError("invalid_operation_payload", "request_digest must be SHA-256")
    elif operation_type == "proposal.review":
        if payload["decision"] not in {"accepted", "rejected"}:
            raise EpiqError("invalid_operation_payload", "Unknown proposal decision")
        accepted = _string_list(
            {"accepted_operation_ids": payload.get("accepted_operation_ids", [])},
            "accepted_operation_ids",
        )
        if payload["decision"] == "rejected" and accepted:
            raise EpiqError(
                "invalid_operation_payload", "Rejected proposal cannot accept operations"
            )
    elif operation_type == "source.bundle_record":
        if not isinstance(payload["request"], dict) or not isinstance(payload["bundle"], dict):
            raise EpiqError(
                "invalid_operation_payload", "Source request and bundle must be objects"
            )
    elif operation_type == "report.record":
        if not isinstance(payload["parameters"], dict):
            raise EpiqError("invalid_operation_payload", "Report parameters must be an object")
        _string_list(payload, "input_transaction_ids")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", payload["content_digest"]):
            raise EpiqError("invalid_operation_payload", "content_digest must be SHA-256")


def apply(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    operation_type: str,
    payload: dict[str, Any],
) -> None:
    validate_payload(operation_type, payload)
    handler = _HANDLERS.get(operation_type)
    if handler:
        handler(connection, transaction_id, operation_id, payload)


def _challenge(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(connection, "projected_questions", "question_id", payload["question_id"])
    if payload.get("example_entity_id"):
        _require_row(
            connection, "projected_entities", "entity_id", payload["example_entity_id"]
        )
    for evidence_id in payload.get("evidence_ids", []):
        _require_row(connection, "projected_evidence", "evidence_id", evidence_id)
    connection.execute(
        "INSERT INTO projected_question_challenges VALUES(?,?,?,?,?,?,?,?,?)",
        (
            payload["challenge_id"],
            payload["question_id"],
            payload["problem"],
            payload["explanation"],
            payload.get("example_entity_id"),
            _json(payload.get("evidence_ids", [])),
            (
                _json(payload["proposed_replacement"])
                if payload.get("proposed_replacement") is not None
                else None
            ),
            transaction_id,
            operation_id,
        ),
    )


def _resolve_challenge(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(
        connection, "projected_question_challenges", "challenge_id", payload["challenge_id"]
    )
    connection.execute(
        "INSERT INTO projected_question_challenge_resolutions VALUES(?,?,?,?,?,?)",
        (
            payload["resolution_id"],
            payload["challenge_id"],
            payload["status"],
            payload["resolution"],
            transaction_id,
            operation_id,
        ),
    )
    decisions = connection.execute(
        """SELECT DISTINCT status,resolution FROM projected_question_challenge_resolutions
           WHERE challenge_id=?""",
        (payload["challenge_id"],),
    ).fetchall()
    if len(decisions) > 1:
        _conflict(
            connection,
            "question_challenge_resolution",
            [payload["challenge_id"]],
            {"decisions": sorted([list(row) for row in decisions])},
        )


def _derivation(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(connection, "projected_claims", "claim_id", payload["claim_id"])
    connection.execute(
        "INSERT INTO projected_derivations VALUES(?,?,?,?,?,?)",
        (
            payload["derivation_id"],
            payload["claim_id"],
            payload["operation"],
            _json(payload["parameters"]),
            transaction_id,
            operation_id,
        ),
    )
    rows = []
    for role, key in (
        ("operand", "input_claim_ids"),
        ("parameter", "parameter_claim_ids"),
        ("path", "path_claim_ids"),
    ):
        for ordinal, claim_id in enumerate(payload[key]):
            if claim_id == payload["claim_id"]:
                raise EpiqError("invalid_derivation", "Derived claim cannot be its own input")
            _require_row(connection, "projected_claims", "claim_id", claim_id)
            rows.append((payload["derivation_id"], claim_id, role, ordinal))
    connection.executemany("INSERT INTO projected_derivation_inputs VALUES(?,?,?,?)", rows)


def _proposal(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO projected_proposals VALUES(?,?,?,?,?,?)",
        (
            payload["proposal_id"],
            payload["proposal_type"],
            _json(payload["document"]),
            payload["request_digest"],
            transaction_id,
            operation_id,
        ),
    )


def _review_proposal(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(connection, "projected_proposals", "proposal_id", payload["proposal_id"])
    accepted = payload.get("accepted_operation_ids", [])
    known_operations: set[str] = set()
    if accepted:
        placeholders = ",".join("?" for _ in accepted)
        query = f"SELECT operation_id FROM ledger_operations WHERE operation_id IN ({placeholders})"
        known_operations = {
            str(row[0]) for row in connection.execute(query, accepted)
        }
    missing = set(accepted) - known_operations
    if missing:
        raise EpiqError("missing_domain_object", f"Missing accepted operations: {sorted(missing)}")
    connection.execute(
        "INSERT INTO projected_proposal_reviews VALUES(?,?,?,?,?,?,?)",
        (
            payload["review_id"],
            payload["proposal_id"],
            payload["decision"],
            payload["reason"],
            _json(accepted),
            transaction_id,
            operation_id,
        ),
    )
    decisions = connection.execute(
        "SELECT DISTINCT decision FROM projected_proposal_reviews WHERE proposal_id=?",
        (payload["proposal_id"],),
    ).fetchall()
    if len(decisions) > 1:
        _conflict(
            connection,
            "proposal_review",
            [payload["proposal_id"]],
            {"decisions": sorted(str(row[0]) for row in decisions)},
        )


def _not_found(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    entity = _require_row(connection, "projected_entities", "entity_id", payload["subject_id"])
    question = _require_row(
        connection, "projected_questions", "question_id", payload["question_id"]
    )
    if entity["kind"] != question["subject_kind"]:
        raise EpiqError("question_kind_mismatch", "Question does not apply to research subject")
    connection.execute(
        "INSERT INTO projected_research_outcomes VALUES(?,?,?,?,?,?,?)",
        (
            payload["outcome_id"],
            payload["subject_id"],
            payload["question_id"],
            payload["query"],
            payload["notes"],
            transaction_id,
            operation_id,
        ),
    )


def _source_bundle(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO projected_source_bundles VALUES(?,?,?,?,?,?)",
        (
            payload["bundle_id"],
            payload["job_id"],
            _json(payload["request"]),
            _json(payload["bundle"]),
            transaction_id,
            operation_id,
        ),
    )


def _report(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    inputs = payload["input_transaction_ids"]
    known: set[str] = set()
    if inputs:
        placeholders = ",".join("?" for _ in inputs)
        query = (
            "SELECT transaction_id FROM ledger_transactions "
            f"WHERE transaction_id IN ({placeholders})"
        )
        known = {str(row[0]) for row in connection.execute(query, inputs)}
    missing = set(payload["input_transaction_ids"]) - known
    if missing:
        raise EpiqError("missing_parent", f"Missing report inputs: {sorted(missing)}")
    connection.execute(
        "INSERT INTO projected_reports VALUES(?,?,?,?,?,?,?)",
        (
            payload["report_id"],
            payload["report_type"],
            _json(payload["parameters"]),
            _json(payload["input_transaction_ids"]),
            payload["content_digest"],
            transaction_id,
            operation_id,
        ),
    )


_HANDLERS = {
    "question.challenge": _challenge,
    "question.challenge_resolve": _resolve_challenge,
    "derivation.declare": _derivation,
    "proposal.create": _proposal,
    "proposal.review": _review_proposal,
    "research.not_found": _not_found,
    "source.bundle_record": _source_bundle,
    "report.record": _report,
}

