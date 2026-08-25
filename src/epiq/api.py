"""Small, semver-covered Python embedding surface for Epiq."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .store import Store


class Database:
    """Stable facade for high-volume writers and claim readers.

    Methods not exposed here remain internal and may change without notice before 1.0.
    """

    def __init__(
        self, path: str | Path, *, create: bool = False, name: str | None = None
    ) -> None:
        self.path = Path(path)
        self._store = Store(self.path)
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
        return self._store.upsert_question(
            name, subject_kind, value_type, definition or {}, actor
        )

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
    path: str | Path, *, create: bool = False, name: str | None = None
) -> Database:
    """Open an Epiq project, optionally initializing it when the path is missing."""
    return Database(path, create=create, name=name)
