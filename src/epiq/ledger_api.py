"""Python facade for Git-native Epiq projects."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ledger import OPERATION_VERSION, LedgerRepository, Operation
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
        if create and not self.path.exists():
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

    def entities(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        return self.view.entities(kind=kind)

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

    def status(self) -> dict[str, Any]:
        return self.writer.projector.build()

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
