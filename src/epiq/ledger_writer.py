"""Ledger-first, crash-recoverable writes for Git-native Epiq projects."""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .errors import EpiqError
from .ledger import LedgerRepository, Operation, Transaction, canonical_bytes
from .ledger_projection import LedgerProjector


def _request_digest(operations: list[Operation] | tuple[Operation, ...]) -> str:
    semantic = [
        {
            "type": operation.type,
            "version": operation.version,
            "requires": list(operation.requires),
            "payload": operation.payload,
            "metadata": operation.metadata,
        }
        for operation in operations
    ]
    return "sha256:" + hashlib.sha256(canonical_bytes(semantic)).hexdigest()


class LedgerWriter:
    """Serialize local writers while keeping the immutable ledger authoritative."""

    def __init__(self, repository: LedgerRepository, *, lock_timeout: float = 10) -> None:
        self.repository = repository
        self.projector = LedgerProjector(repository)
        self.lock_timeout = lock_timeout
        self.lock_path = repository.root / ".epiq" / "write.lock"

    def commit(
        self,
        operations: list[Operation] | tuple[Operation, ...],
        *,
        actor: str,
        recorded_at: str,
        idempotency_key: str | None = None,
        parents: list[str] | tuple[str, ...] | None = None,
        transaction_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        after_append: Callable[[Transaction], None] | None = None,
    ) -> dict[str, Any]:
        if not operations:
            raise EpiqError("empty_transaction", "Ledger write requires at least one operation")
        request_digest = _request_digest(operations)
        with self._lock():
            self.projector.build()
            if idempotency_key:
                existing = self._idempotent_result(actor, idempotency_key, request_digest)
                if existing is not None:
                    projection = self.projector.build()
                    return {
                        "transaction": existing,
                        "projection": projection,
                        "created": False,
                    }
            transaction_metadata = dict(metadata or {})
            if "request_digest" in transaction_metadata:
                raise EpiqError(
                    "reserved_metadata", "request_digest is managed by the ledger writer"
                )
            transaction_metadata["request_digest"] = request_digest
            transaction = Transaction.create(
                self.repository.config.project_id,
                self.repository.config.replica_id,
                actor,
                operations,
                recorded_at=recorded_at,
                parents=self.repository.frontier() if parents is None else parents,
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
                metadata=transaction_metadata,
            )
            self.projector.dry_run(transaction)
            created = self.repository.append(transaction)
            if after_append is not None:
                after_append(transaction)
            projection = self.projector.build()
            return {
                "transaction": transaction,
                "projection": projection,
                "created": created,
            }

    def _idempotent_result(
        self, actor: str, idempotency_key: str, request_digest: str
    ) -> Transaction | None:
        matches = [
            transaction
            for transaction in self.repository.transactions()
            if transaction.actor == actor and transaction.idempotency_key == idempotency_key
        ]
        if len(matches) > 1:
            raise EpiqError(
                "duplicate_idempotency_key",
                f"Multiple transactions use idempotency key {idempotency_key!r} for {actor}",
            )
        if not matches:
            return None
        existing = matches[0]
        if existing.metadata.get("request_digest") != request_digest:
            raise EpiqError(
                "idempotency_conflict",
                f"Idempotency key {idempotency_key!r} was used for another request",
            )
        return existing

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if time.monotonic() >= deadline:
                        raise EpiqError(
                            "ledger_write_locked",
                            f"Timed out waiting for project writer lock: {self.lock_path}",
                        ) from error
                    time.sleep(0.01)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

