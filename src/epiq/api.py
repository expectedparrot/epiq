"""Small, semver-covered Python embedding surface for Epiq."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .store import Store


class Database:
    """Stable facade for high-volume writers and claim readers.

    Methods not exposed here remain internal and may change without notice before 1.0.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        create: bool = False,
        name: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self._store = Store(self.path, clock=clock)
        if create and not self.path.exists():
            self._store.initialize(name or self.path.stem)
            return
        if not self.path.exists():
            raise EpiqError(
                "uninitialized_database",
                f"Epiq project does not exist: {self.path}",
                "Pass create=True and optionally name=... to initialize it.",
            )
        try:
            self._store.overview()
        except sqlite3.OperationalError as error:
            raise EpiqError(
                "uninitialized_database",
                f"File is not an initialized Epiq project: {self.path}",
                "Use a different path, or remove the file and reopen with create=True.",
            ) from error
        except sqlite3.DatabaseError as error:
            raise EpiqError(
                "invalid_database",
                f"Could not read Epiq project: {self.path}",
                "Restore a valid Epiq SQLite project or choose another path.",
            ) from error

    def batch_write(
        self, operations: list[dict[str, Any]], *, actor: str = "agent:python"
    ) -> list[dict[str, Any]]:
        """Atomically add evidence and claims using the batch-write 1.0 contract."""
        return self._store.write_batch(operations, actor)

    def review_claim(
        self, claim_id: str, decision: str, reason: str, *, actor: str = "human:python"
    ) -> str:
        """Record a procedural review without changing the asserted value."""
        return self._store.review_claim(claim_id, decision, reason, actor)

    def provenance(self) -> dict[str, Any]:
        """Inspect capture, claim support, and explicit review gaps."""
        return self._store.provenance_diagnostics()

    def source_bundles(self, *, job_id: str | None = None) -> list[dict[str, Any]]:
        return self._store.source_bundles(job_id)

    def propose(
        self,
        document: dict[str, Any],
        *,
        actor: str = "agent:python",
        idempotency_key: str | None = None,
    ) -> str:
        """Validate and stage a complete document for atomic human review."""
        return self._store.propose_document(document, actor, idempotency_key)

    def proposals(self, *, status: str | None = "pending") -> list[dict[str, Any]]:
        """Read staged documents and their immutable original requests."""
        return self._store.document_proposals(status)

    def review_proposal(
        self, proposal_id: str, decision: str, reason: str, *, actor: str = "human:python"
    ) -> dict[str, Any]:
        """Approve or reject a staged document; repeating a decision is a safe retry."""
        return self._store.review_document_proposal(proposal_id, decision, reason, actor)

    def import_document(
        self,
        document: dict[str, Any],
        *,
        recorded_at: str,
        actor: str = "import:python",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Atomically append a historical phase with consistent transaction timestamps."""
        return self._store.import_document(document, recorded_at, actor, idempotency_key)

    def upsert_entity(
        self,
        kind: str,
        name: str,
        *,
        actor: str = "agent:python",
        attributes: dict[str, Any] | None = None,
        role: str = "entity",
        identity: dict[str, Any] | None = None,
    ) -> str:
        """Create an entity, idempotently when a compound identity is supplied."""
        return self._store.upsert_entity(kind, name, attributes, actor, role, identity)

    def define_question(
        self,
        name: str,
        subject_kind: str,
        value_type: str,
        *,
        actor: str = "agent:python",
        definition: dict[str, Any] | None = None,
    ) -> str:
        """Define a new immutable question version."""
        return self._store.upsert_question(name, subject_kind, value_type, definition or {}, actor)

    def matrix(
        self,
        kind: str,
        *,
        questions: list[str] | None = None,
        known_at: str | None = None,
        valid_at: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Read a consistent current or bitemporal snapshot."""
        return self._store.matrix(kind, questions, known_at, valid_at, actor)

    def claims(
        self,
        *,
        kind: str | None = None,
        question: str | None = None,
        actor: str | None = None,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Read claims in lossless long form with assertion actor attribution."""
        return self._store.list_claims(kind, question, actor, include_superseded)


def open_database(
    path: str | Path,
    *,
    create: bool = False,
    name: str | None = None,
    clock: Callable[[], str] | None = None,
) -> Database:
    """Open an Epiq project, optionally initializing it when the path is missing."""
    return Database(path, create=create, name=name, clock=clock)
