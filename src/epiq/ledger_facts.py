"""Typed evidence and claim reducers for the Git-native ledger."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .errors import EpiqError
from .ledger_domain import _conflict, _json, _require_row
from .temporal import normalize

FACT_SCHEMA = """
CREATE TABLE projected_evidence (
    evidence_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    published_at TEXT,
    excerpt TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    source_entity_id TEXT REFERENCES projected_entities(entity_id),
    capture_json TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    created_operation_id TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_projected_evidence_hash ON projected_evidence(content_hash,evidence_id);

CREATE TABLE projected_evidence_assessments (
    assessment_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES projected_evidence(evidence_id),
    status TEXT NOT NULL CHECK(status IN ('accepted','disputed','invalid','superseded')),
    reason TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_claims (
    claim_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES projected_entities(entity_id),
    question_id TEXT NOT NULL REFERENCES projected_questions(question_id),
    value_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK(confidence IN ('low','medium','high')),
    temporal_basis TEXT NOT NULL CHECK(temporal_basis IN ('observed','source','unknown')),
    epistemic_status TEXT NOT NULL
        CHECK(epistemic_status IN ('observed','reported','derived','estimated')),
    estimate_json TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    created_operation_id TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_projected_claim_cell
ON projected_claims(subject_id,question_id,valid_from,claim_id);

CREATE TABLE projected_claim_supports (
    claim_id TEXT NOT NULL REFERENCES projected_claims(claim_id),
    evidence_id TEXT NOT NULL REFERENCES projected_evidence(evidence_id),
    exact_quote TEXT,
    locator_json TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL,
    PRIMARY KEY(claim_id,evidence_id,operation_id)
);

CREATE TABLE projected_claim_closures (
    claim_id TEXT NOT NULL REFERENCES projected_claims(claim_id),
    closure_type TEXT NOT NULL CHECK(closure_type IN ('retracted','superseded')),
    replacement_claim_id TEXT REFERENCES projected_claims(claim_id),
    reason TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT PRIMARY KEY
);

CREATE TABLE projected_claim_validity_ends (
    claim_id TEXT NOT NULL REFERENCES projected_claims(claim_id),
    valid_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT PRIMARY KEY
);

CREATE TABLE projected_claim_reviews (
    review_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES projected_claims(claim_id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    reason TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT NOT NULL UNIQUE
);

CREATE VIEW projected_active_claims AS
SELECT c.* FROM projected_claims c
WHERE NOT EXISTS (
    SELECT 1 FROM projected_claim_closures closure WHERE closure.claim_id=c.claim_id
);
"""

_SOURCE_TYPES = {"web", "personal", "model", "report", "interview", "other"}
_SPECS: dict[str, tuple[set[str], set[str]]] = {
    "evidence.capture": (
        {
            "evidence_id",
            "source_id",
            "url",
            "source_type",
            "title",
            "retrieved_at",
            "excerpt",
            "content_hash",
        },
        {"published_at", "locator", "source_entity_id", "capture"},
    ),
    "evidence.assess": ({"assessment_id", "evidence_id", "status", "reason"}, set()),
    "claim.assert": (
        {
            "claim_id",
            "subject_id",
            "question_id",
            "value",
            "valid_from",
            "evidence_ids",
            "confidence",
            "temporal_basis",
        },
        {"epistemic_status", "estimate"},
    ),
    "claim.support": ({"claim_id", "evidence_id"}, {"exact_quote", "locator"}),
    "claim.supersede": ({"claim_id", "replacement_claim_id", "reason"}, set()),
    "claim.retract": ({"claim_id", "reason"}, set()),
    "claim.validity_end": ({"claim_id", "valid_to", "reason"}, set()),
    "claim.review": ({"review_id", "claim_id", "decision", "reason"}, set()),
}


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EpiqError("invalid_operation_payload", f"{key} must be a non-empty string")
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
    string_keys = required - {"value", "evidence_ids"}
    for key in string_keys:
        _string(payload, key)
    if operation_type == "evidence.capture":
        if payload["source_type"] not in _SOURCE_TYPES:
            raise EpiqError("invalid_operation_payload", "Unknown evidence source_type")
        normalize(payload["retrieved_at"], field="retrieved_at")
        if payload.get("published_at") is not None:
            normalize(payload["published_at"], field="published_at")
        if not re.fullmatch(r"[0-9a-f]{64}", payload["content_hash"]):
            raise EpiqError("invalid_operation_payload", "content_hash must be lowercase SHA-256")
        for key in ("locator", "capture"):
            if not isinstance(payload.get(key, {}), dict):
                raise EpiqError("invalid_operation_payload", f"{key} must be an object")
    elif operation_type == "evidence.assess":
        if payload["status"] not in {"accepted", "disputed", "invalid", "superseded"}:
            raise EpiqError("invalid_operation_payload", "Unknown evidence assessment")
    elif operation_type == "claim.assert":
        evidence = payload["evidence_ids"]
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item for item in evidence)
            or len(evidence) != len(set(evidence))
        ):
            raise EpiqError("invalid_operation_payload", "evidence_ids must be unique strings")
        normalize(payload["valid_from"], field="valid_from")
        if payload["confidence"] not in {"low", "medium", "high"}:
            raise EpiqError("invalid_operation_payload", "Unknown claim confidence")
        if payload["temporal_basis"] not in {"observed", "source", "unknown"}:
            raise EpiqError("invalid_operation_payload", "Unknown temporal_basis")
        if payload.get("epistemic_status", "reported") not in {
            "observed",
            "reported",
            "derived",
            "estimated",
        }:
            raise EpiqError("invalid_operation_payload", "Unknown epistemic_status")
        if not isinstance(payload.get("estimate", {}), dict):
            raise EpiqError("invalid_operation_payload", "estimate must be an object")
    elif operation_type == "claim.support":
        if payload.get("exact_quote") is not None:
            _string(payload, "exact_quote")
        if not isinstance(payload.get("locator", {}), dict):
            raise EpiqError("invalid_operation_payload", "locator must be an object")
    elif operation_type == "claim.validity_end":
        normalize(payload["valid_to"], field="valid_to")
    elif operation_type == "claim.review" and payload["decision"] not in {
        "accepted",
        "rejected",
    }:
        raise EpiqError("invalid_operation_payload", "Unknown claim review decision")


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


def _capture(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    if payload.get("source_entity_id") is not None:
        _require_row(
            connection, "projected_entities", "entity_id", payload["source_entity_id"]
        )
    try:
        connection.execute(
            "INSERT INTO projected_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["evidence_id"],
                payload["source_id"],
                payload["url"],
                payload["source_type"],
                payload["title"],
                normalize(payload["retrieved_at"], field="retrieved_at"),
                (
                    normalize(payload["published_at"], field="published_at")
                    if payload.get("published_at") is not None
                    else None
                ),
                payload["excerpt"],
                payload["content_hash"],
                _json(payload.get("locator") or {}),
                payload.get("source_entity_id"),
                _json(payload.get("capture") or {}),
                transaction_id,
                operation_id,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise EpiqError(
            "domain_id_collision", f"Evidence ID is already defined: {payload['evidence_id']}"
        ) from error
    matches = connection.execute(
        "SELECT evidence_id FROM projected_evidence WHERE content_hash=? AND evidence_id<>?",
        (payload["content_hash"], payload["evidence_id"]),
    ).fetchall()
    for match in matches:
        _conflict(
            connection,
            "evidence_duplicate",
            [str(match[0]), payload["evidence_id"]],
            {"content_hash": payload["content_hash"]},
        )


def _assess(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(connection, "projected_evidence", "evidence_id", payload["evidence_id"])
    connection.execute(
        "INSERT INTO projected_evidence_assessments VALUES(?,?,?,?,?,?)",
        (
            payload["assessment_id"],
            payload["evidence_id"],
            payload["status"],
            payload["reason"],
            transaction_id,
            operation_id,
        ),
    )


def _assert_claim(
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
        raise EpiqError("question_kind_mismatch", "Question does not apply to claim subject")
    for evidence_id in payload["evidence_ids"]:
        _require_row(connection, "projected_evidence", "evidence_id", evidence_id)
    try:
        connection.execute(
            "INSERT INTO projected_claims VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                payload["claim_id"],
                payload["subject_id"],
                payload["question_id"],
                _json(payload["value"]),
                normalize(payload["valid_from"], field="valid_from"),
                payload["confidence"],
                payload["temporal_basis"],
                payload.get("epistemic_status", "reported"),
                _json(payload.get("estimate") or {}),
                transaction_id,
                operation_id,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise EpiqError(
            "domain_id_collision", f"Claim ID is already defined: {payload['claim_id']}"
        ) from error
    connection.executemany(
        "INSERT INTO projected_claim_supports VALUES(?,?,?,?,?,?)",
        [
            (payload["claim_id"], evidence_id, None, "{}", transaction_id, operation_id)
            for evidence_id in payload["evidence_ids"]
        ],
    )


def _support(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(connection, "projected_claims", "claim_id", payload["claim_id"])
    _require_row(connection, "projected_evidence", "evidence_id", payload["evidence_id"])
    connection.execute(
        "INSERT INTO projected_claim_supports VALUES(?,?,?,?,?,?)",
        (
            payload["claim_id"],
            payload["evidence_id"],
            payload.get("exact_quote"),
            _json(payload.get("locator") or {}),
            transaction_id,
            operation_id,
        ),
    )


def _close(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
    *,
    closure_type: str,
) -> None:
    _require_row(connection, "projected_claims", "claim_id", payload["claim_id"])
    replacement = payload.get("replacement_claim_id")
    if replacement is not None:
        _require_row(connection, "projected_claims", "claim_id", replacement)
    connection.execute(
        "INSERT INTO projected_claim_closures VALUES(?,?,?,?,?,?)",
        (
            payload["claim_id"],
            closure_type,
            replacement,
            payload["reason"],
            transaction_id,
            operation_id,
        ),
    )
    closures = connection.execute(
        """SELECT closure_type,COALESCE(replacement_claim_id,'')
           FROM projected_claim_closures WHERE claim_id=?""",
        (payload["claim_id"],),
    ).fetchall()
    decisions = {tuple(row) for row in closures}
    if len(decisions) > 1:
        _conflict(
            connection,
            "claim_closure",
            [payload["claim_id"], *(item[1] for item in decisions if item[1])],
            {"decisions": sorted([list(item) for item in decisions])},
        )


def _end_validity(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(connection, "projected_claims", "claim_id", payload["claim_id"])
    connection.execute(
        "INSERT INTO projected_claim_validity_ends VALUES(?,?,?,?,?)",
        (
            payload["claim_id"],
            normalize(payload["valid_to"], field="valid_to"),
            payload["reason"],
            transaction_id,
            operation_id,
        ),
    )
    endings = connection.execute(
        "SELECT DISTINCT valid_to FROM projected_claim_validity_ends WHERE claim_id=?",
        (payload["claim_id"],),
    ).fetchall()
    if len(endings) > 1:
        _conflict(
            connection,
            "claim_validity_end",
            [payload["claim_id"]],
            {"valid_to": sorted(str(row[0]) for row in endings)},
        )


def _review(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    _require_row(connection, "projected_claims", "claim_id", payload["claim_id"])
    connection.execute(
        "INSERT INTO projected_claim_reviews VALUES(?,?,?,?,?,?)",
        (
            payload["review_id"],
            payload["claim_id"],
            payload["decision"],
            payload["reason"],
            transaction_id,
            operation_id,
        ),
    )
    decisions = connection.execute(
        "SELECT DISTINCT decision FROM projected_claim_reviews WHERE claim_id=?",
        (payload["claim_id"],),
    ).fetchall()
    if len(decisions) > 1:
        _conflict(
            connection,
            "claim_review",
            [payload["claim_id"]],
            {"decisions": sorted(str(row[0]) for row in decisions)},
        )


_HANDLERS = {
    "evidence.capture": _capture,
    "evidence.assess": _assess,
    "claim.assert": _assert_claim,
    "claim.support": _support,
    "claim.supersede": lambda connection, transaction, operation, payload: _close(
        connection, transaction, operation, payload, closure_type="superseded"
    ),
    "claim.retract": lambda connection, transaction, operation, payload: _close(
        connection, transaction, operation, payload, closure_type="retracted"
    ),
    "claim.validity_end": _end_validity,
    "claim.review": _review,
}

