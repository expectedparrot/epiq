"""Atomic proposal bundles and actor-scoped retry receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from .errors import EpiqError
from .provenance import review_claim, validate_document_retention

if TYPE_CHECKING:
    from .store import Store


def encoded(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise EpiqError("invalid_document", "Requests must contain finite JSON values") from error


class Receipt:
    """Used only under the same immediate transaction as the recorded mutation."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        namespace: str,
        actor: str,
        key: str | None,
        request: Any,
    ):
        self.connection = connection
        self.request = encoded(request)
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise EpiqError("invalid_idempotency_key", "Idempotency key must be a nonempty string")
        self.identity = (
            namespace,
            actor,
            key if key is not None else hashlib.sha256(self.request.encode()).hexdigest(),
        )
        row = connection.execute(
            "SELECT request_json,result_json FROM write_receipts "
            "WHERE namespace=? AND actor=? AND idempotency_key=?",
            self.identity,
        ).fetchone()
        self.result = None
        if row:
            if row["request_json"] != self.request:
                raise EpiqError(
                    "idempotency_conflict",
                    "This actor already used the key for a different request",
                )
            self.result = json.loads(row["result_json"])

    def save(self, result: Any) -> Any:
        self.connection.execute(
            "INSERT INTO write_receipts VALUES(?,?,?,?,?)",
            (*self.identity, self.request, encoded(result)),
        )
        return result


def propose(
    store: Store, document: dict[str, Any], actor: str, idempotency_key: str | None = None
) -> str:
    validate_document_retention(document)
    with store.transaction() as connection:
        receipt = Receipt(connection, "document.propose", actor, idempotency_key, document)
        if receipt.result is not None:
            return receipt.result
        # Exercise the exact acceptance path, then discard every validation write.
        connection.execute("SAVEPOINT candidate")
        try:
            store._apply_document_tx(connection, document, actor)
        finally:
            connection.execute("ROLLBACK TO candidate")
            connection.execute("RELEASE candidate")
        proposal_id = "prp_" + uuid.uuid4().hex[:16]
        seq, _, _ = store._event(
            connection,
            "document.propose",
            actor,
            {
                "proposal_id": proposal_id,
                "document": document,
                "idempotency_key": receipt.identity[2],
            },
        )
        connection.execute(
            "INSERT INTO document_proposals VALUES(?,?,'pending',NULL,NULL,?,NULL)",
            (proposal_id, receipt.request, seq),
        )
        return receipt.save(proposal_id)


def proposals(store: Store, status: str | None = "pending") -> list[dict[str, Any]]:
    if status not in {None, "pending", "approved", "rejected"}:
        raise EpiqError("invalid_proposal_status", f"Unknown proposal status: {status}")
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT p.*,e.actor,e.recorded_at FROM document_proposals p "
            "JOIN events e ON e.seq=p.created_seq "
            "WHERE (? IS NULL OR p.status=?) ORDER BY p.created_seq",
            (status, status),
        ).fetchall()
    return [
        dict(row)
        | {
            "document": json.loads(row["document_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }
        for row in rows
    ]


def review(
    store: Store, proposal_id: str, decision: str, reason: str, actor: str
) -> dict[str, Any]:
    if decision not in {"approved", "rejected"}:
        raise EpiqError("invalid_review_decision", f"Unknown decision: {decision}")
    if not reason.strip():
        raise EpiqError("reason_required", "A review reason is required")
    with store.transaction() as connection:
        proposal = connection.execute(
            "SELECT * FROM document_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if proposal is None:
            raise EpiqError("proposal_not_found", f"Proposal not found: {proposal_id}")
        if proposal["status"] != "pending":
            if proposal["status"] != decision:
                raise EpiqError(
                    "proposal_already_reviewed", "A reviewed proposal cannot change decision"
                )
            return json.loads(proposal["result_json"])
        applied = None
        if decision == "approved":
            applied = store._apply_document_tx(
                connection, json.loads(proposal["document_json"]), actor
            )
            for operation in applied["operations"]:
                if operation["op"] == "claim.assert":
                    review_claim(
                        store, connection, operation["claim_id"], "accepted", reason, actor
                    )
        result = {"proposal_id": proposal_id, "status": decision, "applied": applied}
        seq, _, _ = store._event(
            connection, "document." + decision, actor, result | {"reason": reason.strip()}
        )
        connection.execute(
            "UPDATE document_proposals SET status=?,result_json=?,reason=?,reviewed_seq=? "
            "WHERE proposal_id=?",
            (decision, encoded(result), reason.strip(), seq, proposal_id),
        )
        return result
