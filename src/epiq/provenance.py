"""Capture provenance and explicit support/review records, independent of inference."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import EpiqError
from .temporal import normalize

MAX_CONTENT = 200_000
SCHEMA = """
CREATE TABLE IF NOT EXISTS source_bundles (
    bundle_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS source_artifacts (
    content_hash TEXT PRIMARY KEY,
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_support (
    support_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    quote TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);
CREATE INDEX IF NOT EXISTS idx_claim_support ON claim_support(claim_id);
CREATE TABLE IF NOT EXISTS claim_reviews (
    review_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected')),
    reason TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);
CREATE INDEX IF NOT EXISTS idx_claim_reviews ON claim_reviews(claim_id,created_seq);
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_update
BEFORE UPDATE ON source_artifacts BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_delete
BEFORE DELETE ON source_artifacts BEGIN SELECT RAISE(ABORT,'artifacts are immutable'); END;
"""


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def capture_metadata(raw: dict[str, Any] | None, excerpt: str) -> tuple[dict[str, Any], str | None]:
    """Verify supplied capture bytes and excerpt membership; never infer truth."""
    if raw is None:
        return {"capture_method": "manual", "verification_status": "unverified"}, None
    allowed = {
        "capture_method",
        "captured_at",
        "provider",
        "content",
        "content_hash",
        "retain_content",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise EpiqError("invalid_provenance", "Unknown capture provenance fields")
    if "retain_content" in raw and not isinstance(raw["retain_content"], bool):
        raise EpiqError("invalid_provenance", "retain_content must be a boolean")
    method = raw.get("capture_method", "manual")
    if method not in {"discovery", "fetched", "manual"}:
        raise EpiqError(
            "invalid_provenance", "Capture method must be discovery, fetched, or manual"
        )
    result = {"capture_method": method, "verification_status": "unverified"}
    if raw.get("provider"):
        if not isinstance(raw["provider"], str) or len(raw["provider"]) > 200:
            raise EpiqError("invalid_provenance", "Provider must be a short name")
        result["provider"] = raw["provider"]
    if raw.get("captured_at"):
        result["captured_at"] = normalize(raw["captured_at"], field="captured_at")
    content = raw.get("content")
    if method == "discovery" and (content is not None or raw.get("content_hash")):
        raise EpiqError("invalid_provenance", "Discovery metadata is not captured source content")
    if method == "fetched" and (content is None or not result.get("captured_at")):
        raise EpiqError("invalid_provenance", "Fetched evidence requires content and captured_at")
    if content is not None:
        if not isinstance(content, str) or not content or len(content) > MAX_CONTENT:
            raise EpiqError(
                "invalid_provenance", "Captured content must contain 1..200000 characters"
            )
        content_hash = digest(content)
        if raw.get("content_hash") and raw["content_hash"] != content_hash:
            raise EpiqError("capture_hash_mismatch", "Capture hash does not match supplied content")
        start = content.find(excerpt)
        if start < 0:
            raise EpiqError(
                "excerpt_not_captured", "Evidence excerpt does not occur in captured content"
            )
        result.update(
            content_hash=content_hash,
            verification_status="excerpt_matched",
            excerpt_start=start,
            excerpt_end=start + len(excerpt),
        )
        if raw.get("retain_content", False):
            result["artifact_hash"] = content_hash
            return result, content
    elif raw.get("content_hash"):
        raise EpiqError("invalid_provenance", "Hash verification requires the captured content")
    return result, None


def add_supports(store, connection, claim_id: str, supports: list[dict], actor: str) -> None:
    if not isinstance(supports, list) or len(supports) > 100:
        raise EpiqError("invalid_support", "Support must be an array of at most 100 spans")
    for support in supports:
        if not isinstance(support, dict) or set(support) - {"evidence_id", "quote", "start", "end"}:
            raise EpiqError(
                "invalid_support", "Support requires evidence_id, quote, and optional offsets"
            )
        evidence_id, quote = support.get("evidence_id"), support.get("quote")
        row = connection.execute(
            "SELECT e.excerpt,ev.payload_json FROM claim_evidence ce "
            "JOIN evidence e ON e.evidence_id=ce.evidence_id JOIN events ev ON "
            "ev.seq=e.created_seq "
            "WHERE ce.claim_id=? AND ce.evidence_id=?",
            (claim_id, evidence_id),
        ).fetchone()
        if row is None:
            raise EpiqError(
                "invalid_support", "Support must reference evidence linked to this claim"
            )
        if not isinstance(quote, str) or not quote:
            raise EpiqError("invalid_support", "Support quote cannot be empty")
        start = support.get("start", row["excerpt"].find(quote))
        end = support.get("end", start + len(quote) if isinstance(start, int) else None)
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end > len(row["excerpt"])
            or row["excerpt"][start:end] != quote
        ):
            raise EpiqError(
                "support_not_in_excerpt", "Support span does not match the evidence excerpt"
            )
        support_id = "sup_" + digest(json.dumps([claim_id, evidence_id, quote, start, end]))[:24]
        if connection.execute(
            "SELECT 1 FROM claim_support WHERE support_id=?", (support_id,)
        ).fetchone():
            continue
        seq, _, _ = store._event(
            connection,
            "claim.support",
            actor,
            {
                "support_id": support_id,
                "claim_id": claim_id,
                "evidence_id": evidence_id,
                "quote": quote,
                "start": start,
                "end": end,
            },
        )
        connection.execute(
            "INSERT INTO claim_support VALUES(?,?,?,?,?,?,?)",
            (support_id, claim_id, evidence_id, quote, start, end, seq),
        )


def review_claim(store, connection, claim_id: str, decision: str, reason: str, actor: str) -> str:
    if decision not in {"accepted", "rejected"} or not reason.strip():
        raise EpiqError("invalid_claim_review", "Review requires accepted/rejected and a reason")
    if not connection.execute("SELECT 1 FROM claims WHERE claim_id=?", (claim_id,)).fetchone():
        raise EpiqError("claim_not_found", f"Claim not found: {claim_id}")
    latest = connection.execute(
        "SELECT r.*,ev.actor FROM claim_reviews r JOIN events ev "
        "ON ev.seq=r.created_seq WHERE claim_id=? ORDER BY created_seq DESC LIMIT 1",
        (claim_id,),
    ).fetchone()
    if latest and (latest["decision"], latest["reason"], latest["actor"]) == (
        decision,
        reason.strip(),
        actor,
    ):
        return latest["review_id"]
    import uuid

    review_id = "rev_" + uuid.uuid4().hex[:16]
    seq, _, _ = store._event(
        connection,
        "claim.review",
        actor,
        {
            "review_id": review_id,
            "claim_id": claim_id,
            "decision": decision,
            "reason": reason.strip(),
        },
    )
    connection.execute(
        "INSERT INTO claim_reviews VALUES(?,?,?,?,?)",
        (review_id, claim_id, decision, reason.strip(), seq),
    )
    return review_id


def diagnostics(store) -> dict[str, Any]:
    """Audit procedural support separately from SQLite/ledger integrity."""
    items = []
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT c.claim_id,ev.actor,ce.evidence_id,se.payload_json,"
            "EXISTS(SELECT 1 FROM claim_support cs WHERE cs.claim_id=c.claim_id AND "
            "cs.evidence_id=ce.evidence_id) has_support,"
            "(SELECT decision FROM claim_reviews r WHERE r.claim_id=c.claim_id ORDER BY "
            "created_seq DESC LIMIT 1) review "
            "FROM claims c JOIN events ev ON ev.seq=c.created_seq "
            "JOIN claim_evidence ce ON ce.claim_id=c.claim_id JOIN evidence e ON "
            "e.evidence_id=ce.evidence_id "
            "JOIN events se ON se.seq=e.created_seq WHERE c.status='asserted' AND c.tx_to IS NULL"
        ).fetchall()
    unreviewed = set()
    for row in rows:
        capture = json.loads(row["payload_json"]).get("provenance", {})
        codes = []
        if capture.get("capture_method") == "discovery":
            codes.append("discovery_only_evidence")
        if capture.get("verification_status") != "excerpt_matched":
            codes.append("unverified_capture")
        if not row["has_support"]:
            codes.append("missing_claim_support")
        if row["review"] != "accepted" and row["claim_id"] not in unreviewed:
            codes.append("rejected_claim" if row["review"] == "rejected" else "unreviewed_claim")
            unreviewed.add(row["claim_id"])
        for code in codes:
            items.append(
                {
                    "code": code,
                    "claim_id": row["claim_id"],
                    "evidence_id": row["evidence_id"],
                    "actor": row["actor"],
                }
            )
    return {
        "scope": "capture_support_and_review",
        "ok": not items,
        "findings": items,
        "note": "Capture matching and human review do not establish semantic truth.",
    }


def validate_document_retention(document):
    if not isinstance(document, dict):
        return
    operations = document.get("operations", [])
    if not isinstance(operations, list):
        return
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        capture = operation.get("provenance", {})
        if (
            isinstance(capture, dict)
            and capture.get("content") is not None
            and not capture.get("retain_content")
        ):
            raise EpiqError(
                "capture_retention_required",
                "Staged or historical full captures require retain_content=True",
                "Direct batch writes can discard content after verification.",
            )
