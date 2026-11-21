"""Python facade for Git-native Epiq projects."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .errors import EpiqError
from .ledger import OPERATION_VERSION, LedgerRepository, Operation, canonical_bytes, new_domain_id
from .ledger_view import LedgerView
from .ledger_writer import LedgerWriter


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class LedgerDatabase:
    """Write immutable transactions and read their disposable projection."""

    def __init__(
        self,
        path: str | Path,
        *,
        create: bool = False,
        name: str | None = None,
        actor: str = "human:python",
        project_id: str | None = None,
        replica_id: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or _now
        if create and not (self.path / "epiq.toml").exists():
            self.repository = LedgerRepository.initialize(
                self.path,
                name or self.path.name,
                actor=actor,
                recorded_at=self._clock(),
                project_id=project_id,
                replica_id=replica_id,
            )
        else:
            self.repository = LedgerRepository(self.path)
            self.repository.validate()
        self.writer = LedgerWriter(self.repository)
        self.view = LedgerView(self.repository)

    def transact(
        self,
        operations: list[dict[str, Any] | Operation],
        *,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate and append one atomic transaction to the authoritative ledger."""
        prepared = [self._operation(item) for item in operations]
        result = self.writer.commit(
            prepared,
            actor=actor,
            recorded_at=recorded_at or self._clock(),
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
        transaction = result["transaction"]
        return {
            "transaction_id": transaction.transaction_id,
            "digest": transaction.digest,
            "created": result["created"],
            "projection": result["projection"],
        }

    def define_kind(
        self,
        kind: str,
        *,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.transact(
            [{"type": "entity_kind.define", "payload": {"kind": kind}}],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"kind": kind, **result}

    def create_entity(
        self,
        kind: str,
        name: str,
        *,
        entity_id: str | None = None,
        attributes: dict[str, Any] | None = None,
        role: str = "entity",
        identity: dict[str, Any] | None = None,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id("ent", entity_id, actor, idempotency_key, "entity")
        payload: dict[str, Any] = {
            "entity_id": identifier,
            "kind": kind,
            "name": name,
            "attributes": attributes or {},
            "role": role,
        }
        if identity is not None:
            payload["identity"] = identity
        result = self.transact(
            [{"type": "entity.create", "payload": payload}],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"entity_id": identifier, **result}

    def define_question(
        self,
        name: str,
        subject_kind: str,
        value_type: str,
        *,
        question_id: str | None = None,
        definition: dict[str, Any] | None = None,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id("q", question_id, actor, idempotency_key, "question")
        result = self.transact(
            [
                {
                    "type": "question.define",
                    "payload": {
                        "question_id": identifier,
                        "name": name,
                        "subject_kind": subject_kind,
                        "value_type": value_type,
                        "definition": definition or {},
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"question_id": identifier, **result}

    def capture_evidence(
        self,
        *,
        source_title: str,
        source_type: str,
        url: str,
        retrieved_at: str,
        excerpt: str,
        source_id: str | None = None,
        evidence_id: str | None = None,
        published_at: str | None = None,
        locator: dict[str, Any] | None = None,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        source = self._domain_id("src", source_id, actor, idempotency_key, "source")
        evidence = self._domain_id("evd", evidence_id, actor, idempotency_key, "evidence")
        payload: dict[str, Any] = {
            "evidence_id": evidence,
            "source_id": source,
            "url": url,
            "source_type": source_type,
            "title": source_title,
            "retrieved_at": retrieved_at,
            "excerpt": excerpt,
            "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
            "locator": locator or {},
        }
        if published_at is not None:
            payload["published_at"] = published_at
        result = self.transact(
            [{"type": "evidence.capture", "payload": payload}],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"source_id": source, "evidence_id": evidence, **result}

    def assert_claim(
        self,
        subject_id: str,
        question_id: str,
        value: Any,
        *,
        valid_from: str,
        evidence_ids: list[str],
        claim_id: str | None = None,
        confidence: str = "high",
        temporal_basis: str = "observed",
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        claim = self._domain_id("clm", claim_id, actor, idempotency_key, "claim")
        result = self.transact(
            [
                {
                    "type": "claim.assert",
                    "payload": {
                        "claim_id": claim,
                        "subject_id": subject_id,
                        "question_id": question_id,
                        "value": value,
                        "valid_from": valid_from,
                        "evidence_ids": evidence_ids,
                        "confidence": confidence,
                        "temporal_basis": temporal_basis,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"claim_id": claim, **result}

    def support_claim(
        self,
        claim_id: str,
        evidence_id: str,
        *,
        exact_quote: str | None = None,
        locator: dict[str, Any] | None = None,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "claim_id": claim_id,
            "evidence_id": evidence_id,
            "locator": locator or {},
        }
        if exact_quote is not None:
            payload["exact_quote"] = exact_quote
        result = self.transact(
            [{"type": "claim.support", "payload": payload}],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"claim_id": claim_id, "evidence_id": evidence_id, **result}

    def supersede_claim(
        self,
        claim_id: str,
        value: Any,
        *,
        valid_from: str,
        evidence_ids: list[str],
        reason: str,
        replacement_claim_id: str | None = None,
        confidence: str = "high",
        temporal_basis: str = "observed",
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        matches = [
            item for item in self.claims(include_closed=True) if item["claim_id"] == claim_id
        ]
        if not matches:
            raise EpiqError("missing_domain_object", f"Missing claim_id: {claim_id}")
        original = matches[0]
        replacement = self._domain_id(
            "clm", replacement_claim_id, actor, idempotency_key, "replacement-claim"
        )
        result = self.transact(
            [
                {
                    "type": "claim.assert",
                    "payload": {
                        "claim_id": replacement,
                        "subject_id": original["subject_id"],
                        "question_id": original["question_id"],
                        "value": value,
                        "valid_from": valid_from,
                        "evidence_ids": evidence_ids,
                        "confidence": confidence,
                        "temporal_basis": temporal_basis,
                    },
                },
                {
                    "type": "claim.supersede",
                    "payload": {
                        "claim_id": claim_id,
                        "replacement_claim_id": replacement,
                        "reason": reason,
                    },
                },
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {
            "claim_id": claim_id,
            "status": "superseded",
            "replacement_claim_id": replacement,
            **result,
        }

    def record(
        self,
        subject_id: str,
        question_id: str,
        value: Any,
        *,
        valid_from: str,
        source_title: str,
        source_type: str,
        url: str,
        retrieved_at: str,
        excerpt: str,
        source_id: str | None = None,
        evidence_id: str | None = None,
        claim_id: str | None = None,
        published_at: str | None = None,
        locator: dict[str, Any] | None = None,
        confidence: str = "medium",
        temporal_basis: str = "observed",
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        source = self._domain_id("src", source_id, actor, idempotency_key, "source")
        evidence = self._domain_id("evd", evidence_id, actor, idempotency_key, "evidence")
        claim = self._domain_id("clm", claim_id, actor, idempotency_key, "claim")
        evidence_payload: dict[str, Any] = {
            "evidence_id": evidence,
            "source_id": source,
            "url": url,
            "source_type": source_type,
            "title": source_title,
            "retrieved_at": retrieved_at,
            "excerpt": excerpt,
            "content_hash": hashlib.sha256(excerpt.encode()).hexdigest(),
            "locator": locator or {},
        }
        if published_at is not None:
            evidence_payload["published_at"] = published_at
        result = self.transact(
            [
                {"type": "evidence.capture", "payload": evidence_payload},
                {
                    "type": "claim.assert",
                    "payload": {
                        "claim_id": claim,
                        "subject_id": subject_id,
                        "question_id": question_id,
                        "value": value,
                        "valid_from": valid_from,
                        "evidence_ids": [evidence],
                        "confidence": confidence,
                        "temporal_basis": temporal_basis,
                    },
                },
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {
            "source_id": source,
            "evidence_id": evidence,
            "claim_id": claim,
            **result,
        }

    def set_entity_visibility(
        self,
        entity_id: str,
        visible: bool,
        reason: str,
        *,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.transact(
            [
                {
                    "type": "entity.restore" if visible else "entity.retire",
                    "payload": {"entity_id": entity_id, "reason": reason},
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"entity_id": entity_id, "visible": visible, **result}

    def add_entity_alias(
        self,
        entity_id: str,
        alias: str,
        *,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.transact(
            [{"type": "entity.alias", "payload": {"entity_id": entity_id, "alias": alias}}],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"entity_id": entity_id, "alias": alias, **result}

    def merge_entities(
        self,
        source_entity_id: str,
        destination_entity_id: str,
        reason: str,
        *,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.transact(
            [
                {
                    "type": "entity.merge",
                    "payload": {
                        "source_entity_id": source_entity_id,
                        "destination_entity_id": destination_entity_id,
                        "reason": reason,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"entity_id": destination_entity_id, "status": "merged", **result}

    def set_question_visibility(
        self,
        question_id: str,
        visible: bool,
        reason: str,
        *,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.transact(
            [
                {
                    "type": "question.restore" if visible else "question.retire",
                    "payload": {"question_id": question_id, "reason": reason},
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"question_id": question_id, "visible": visible, **result}

    def evolve_question(
        self,
        predecessor_question_id: str,
        replacements: list[dict[str, Any]],
        relationship: str,
        reason: str,
        *,
        retire_predecessor: bool = True,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not replacements:
            raise ValueError("question evolution requires at least one replacement")
        operations: list[dict[str, Any]] = []
        successor_ids: list[str] = []
        for index, replacement in enumerate(replacements):
            required = {"name", "subject_kind", "value_type"}
            unknown = set(replacement) - required - {"question_id", "definition"}
            missing = required - set(replacement)
            if missing or unknown:
                raise ValueError(
                    f"replacement {index} missing={sorted(missing)} unknown={sorted(unknown)}"
                )
            successor_id = self._domain_id(
                "q",
                replacement.get("question_id"),
                actor,
                idempotency_key,
                f"question-successor-{index}",
            )
            successor_ids.append(successor_id)
            operations.append(
                {
                    "type": "question.define",
                    "payload": {
                        "question_id": successor_id,
                        "name": replacement["name"],
                        "subject_kind": replacement["subject_kind"],
                        "value_type": replacement["value_type"],
                        "definition": replacement.get("definition", {}),
                    },
                }
            )
        operations.append(
            {
                "type": "question.evolve",
                "payload": {
                    "predecessor_question_id": predecessor_question_id,
                    "successor_question_ids": successor_ids,
                    "relationship": relationship,
                    "reason": reason,
                },
            }
        )
        if retire_predecessor:
            operations.append(
                {
                    "type": "question.retire",
                    "payload": {"question_id": predecessor_question_id, "reason": reason},
                }
            )
        result = self.transact(
            operations,
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"successor_question_ids": successor_ids, **result}

    def assess_evidence(
        self,
        evidence_id: str,
        status: str,
        reason: str,
        *,
        assessment_id: str | None = None,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id(
            "asm", assessment_id, actor, idempotency_key, "assessment"
        )
        result = self.transact(
            [
                {
                    "type": "evidence.assess",
                    "payload": {
                        "assessment_id": identifier,
                        "evidence_id": evidence_id,
                        "status": status,
                        "reason": reason,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"assessment_id": identifier, "status": status, **result}

    def review_claim(
        self,
        claim_id: str,
        decision: str,
        reason: str,
        *,
        review_id: str | None = None,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id("rev", review_id, actor, idempotency_key, "review")
        result = self.transact(
            [
                {
                    "type": "claim.review",
                    "payload": {
                        "review_id": identifier,
                        "claim_id": claim_id,
                        "decision": decision,
                        "reason": reason,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"review_id": identifier, "decision": decision, **result}

    def retract_claim(
        self,
        claim_id: str,
        reason: str,
        *,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.transact(
            [{"type": "claim.retract", "payload": {"claim_id": claim_id, "reason": reason}}],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"claim_id": claim_id, "status": "retracted", **result}

    def end_claim_validity(
        self,
        claim_id: str,
        valid_to: str,
        reason: str,
        *,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.transact(
            [
                {
                    "type": "claim.validity_end",
                    "payload": {"claim_id": claim_id, "valid_to": valid_to, "reason": reason},
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"claim_id": claim_id, "valid_to": valid_to, **result}

    def challenge_question(
        self,
        question_id: str,
        problem: str,
        explanation: str,
        *,
        challenge_id: str | None = None,
        example_entity_id: str | None = None,
        evidence_ids: list[str] | None = None,
        proposed_replacement: dict[str, Any] | None = None,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id(
            "chg", challenge_id, actor, idempotency_key, "question-challenge"
        )
        payload: dict[str, Any] = {
            "challenge_id": identifier,
            "question_id": question_id,
            "problem": problem,
            "explanation": explanation,
            "evidence_ids": evidence_ids or [],
        }
        if example_entity_id is not None:
            payload["example_entity_id"] = example_entity_id
        if proposed_replacement is not None:
            payload["proposed_replacement"] = proposed_replacement
        result = self.transact(
            [{"type": "question.challenge", "payload": payload}],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"challenge_id": identifier, "status": "open", **result}

    def resolve_question_challenge(
        self,
        challenge_id: str,
        status: str,
        resolution: str,
        *,
        resolution_id: str | None = None,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id(
            "res", resolution_id, actor, idempotency_key, "challenge-resolution"
        )
        result = self.transact(
            [
                {
                    "type": "question.challenge_resolve",
                    "payload": {
                        "resolution_id": identifier,
                        "challenge_id": challenge_id,
                        "status": status,
                        "resolution": resolution,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"resolution_id": identifier, "status": status, **result}

    def record_not_found(
        self,
        subject_id: str,
        question_id: str,
        query: str,
        notes: str,
        *,
        outcome_id: str | None = None,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id(
            "out", outcome_id, actor, idempotency_key, "research-outcome"
        )
        result = self.transact(
            [
                {
                    "type": "research.not_found",
                    "payload": {
                        "outcome_id": identifier,
                        "subject_id": subject_id,
                        "question_id": question_id,
                        "query": query,
                        "notes": notes,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"outcome_id": identifier, "state": "NotFound", **result}

    def derive_claim(
        self,
        subject_id: str,
        question_id: str,
        value: Any,
        *,
        valid_from: str,
        evidence_ids: list[str],
        operation: str,
        input_claim_ids: list[str],
        parameter_claim_ids: list[str] | None = None,
        path_claim_ids: list[str] | None = None,
        parameters: dict[str, Any] | None = None,
        claim_id: str | None = None,
        derivation_id: str | None = None,
        confidence: str = "medium",
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        claim = self._domain_id("clm", claim_id, actor, idempotency_key, "derived-claim")
        derivation = self._domain_id(
            "drv", derivation_id, actor, idempotency_key, "derivation"
        )
        result = self.transact(
            [
                {
                    "type": "claim.assert",
                    "payload": {
                        "claim_id": claim,
                        "subject_id": subject_id,
                        "question_id": question_id,
                        "value": value,
                        "valid_from": valid_from,
                        "evidence_ids": evidence_ids,
                        "confidence": confidence,
                        "temporal_basis": "observed",
                    },
                },
                {
                    "type": "derivation.declare",
                    "payload": {
                        "derivation_id": derivation,
                        "claim_id": claim,
                        "operation": operation,
                        "input_claim_ids": input_claim_ids,
                        "parameter_claim_ids": parameter_claim_ids or [],
                        "path_claim_ids": path_claim_ids or [],
                        "parameters": parameters or {},
                    },
                },
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"claim_id": claim, "derivation_id": derivation, **result}

    def create_proposal(
        self,
        document: dict[str, Any],
        *,
        proposal_type: str = "operations",
        proposal_id: str | None = None,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id("prp", proposal_id, actor, idempotency_key, "proposal")
        request_digest = "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()
        result = self.transact(
            [
                {
                    "type": "proposal.create",
                    "payload": {
                        "proposal_id": identifier,
                        "proposal_type": proposal_type,
                        "document": document,
                        "request_digest": request_digest,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"proposal_id": identifier, "request_digest": request_digest, **result}

    def review_proposal(
        self,
        proposal_id: str,
        decision: str,
        reason: str,
        *,
        accepted_operation_ids: list[str] | None = None,
        review_id: str | None = None,
        actor: str = "human:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id(
            "rev", review_id, actor, idempotency_key, "proposal-review"
        )
        result = self.transact(
            [
                {
                    "type": "proposal.review",
                    "payload": {
                        "review_id": identifier,
                        "proposal_id": proposal_id,
                        "decision": decision,
                        "reason": reason,
                        "accepted_operation_ids": accepted_operation_ids or [],
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"review_id": identifier, "decision": decision, **result}

    def record_source_bundle(
        self,
        job_id: str,
        request: dict[str, Any],
        bundle: dict[str, Any],
        *,
        bundle_id: str | None = None,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        identifier = self._domain_id("bnd", bundle_id, actor, idempotency_key, "source-bundle")
        result = self.transact(
            [
                {
                    "type": "source.bundle_record",
                    "payload": {
                        "bundle_id": identifier,
                        "job_id": job_id,
                        "request": request,
                        "bundle": bundle,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"bundle_id": identifier, **result}

    def record_report(
        self,
        report_type: str,
        content: bytes,
        *,
        parameters: dict[str, Any] | None = None,
        input_transaction_ids: list[str] | None = None,
        retention: str = "git",
        report_id: str | None = None,
        actor: str = "agent:python",
        recorded_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        artifact = ArtifactStore(self.path).retain(content, retention=retention)
        identifier = self._domain_id("rpt", report_id, actor, idempotency_key, "report")
        report_parameters = dict(parameters or {})
        if "artifact_retention" in report_parameters:
            raise ValueError("artifact_retention is managed by record_report")
        report_parameters["artifact_retention"] = retention
        result = self.transact(
            [
                {
                    "type": "report.record",
                    "payload": {
                        "report_id": identifier,
                        "report_type": report_type,
                        "parameters": report_parameters,
                        "input_transaction_ids": input_transaction_ids or [],
                        "content_digest": artifact.digest,
                    },
                }
            ],
            actor=actor,
            recorded_at=recorded_at,
            idempotency_key=idempotency_key,
        )
        return {"report_id": identifier, "artifact": artifact.as_dict(), **result}

    def entities(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        return self.view.entities(kind=kind)

    def project(self) -> dict[str, Any]:
        return self.view.project()

    def kinds(self) -> list[str]:
        return self.view.kinds()

    def questions(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        return self.view.questions(kind=kind)

    def evidence(self, *, evidence_ids: list[str] | None = None) -> list[dict[str, Any]]:
        return self.view.evidence(evidence_ids=evidence_ids)

    def claims(
        self,
        *,
        kind: str | None = None,
        question: str | None = None,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        return self.view.claims(
            kind=kind,
            question=question,
            include_closed=include_closed,
        )

    def matrix(self, kind: str) -> dict[str, Any]:
        return self.view.matrix(kind)

    def conflicts(self, *, conflict_type: str | None = None) -> list[dict[str, Any]]:
        return self.view.conflicts(conflict_type=conflict_type)

    def question_challenges(
        self, *, question: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        return self.view.question_challenges(question=question, status=status)

    def research_outcomes(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        return self.view.research_outcomes(kind=kind)

    def derivations(self) -> list[dict[str, Any]]:
        return self.view.derivations()

    def proposals(self) -> list[dict[str, Any]]:
        return self.view.proposals()

    def source_bundles(self) -> list[dict[str, Any]]:
        return self.view.source_bundles()

    def reports(self) -> list[dict[str, Any]]:
        return self.view.reports()

    def status(self) -> dict[str, Any]:
        return self.writer.projector.build()

    def _domain_id(
        self,
        prefix: str,
        supplied: str | None,
        actor: str,
        idempotency_key: str | None,
        role: str,
    ) -> str:
        if supplied is not None:
            return supplied
        if idempotency_key is None:
            return new_domain_id(prefix)
        material = f"{self.repository.config.project_id}\n{actor}\n{idempotency_key}\n{role}"
        return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:24]}"

    @staticmethod
    def _operation(value: dict[str, Any] | Operation) -> Operation:
        if isinstance(value, Operation):
            return value
        if not isinstance(value, dict):
            raise TypeError("operations must contain Operation or dictionary values")
        unknown = set(value) - {"type", "payload", "version", "requires", "metadata"}
        if unknown or "type" not in value or "payload" not in value:
            raise ValueError(
                "operation dictionaries require type and payload; "
                f"unknown keys: {sorted(unknown)}"
            )
        if value.get("version", OPERATION_VERSION) != OPERATION_VERSION:
            raise ValueError(f"unsupported operation version: {value['version']}")
        return Operation.create(
            value["type"],
            value["payload"],
            requires=value.get("requires", ()),
            metadata=value.get("metadata"),
        )


def open_ledger(
    path: str | Path,
    *,
    create: bool = False,
    name: str | None = None,
    actor: str = "human:python",
    project_id: str | None = None,
    replica_id: str | None = None,
    clock: Callable[[], str] | None = None,
) -> LedgerDatabase:
    """Open or initialize a Git-native Epiq project directory."""
    return LedgerDatabase(
        path,
        create=create,
        name=name,
        actor=actor,
        project_id=project_id,
        replica_id=replica_id,
        clock=clock,
    )
