"""Deterministic disposable SQLite projection of an Epiq Git ledger."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .ledger import LedgerRepository, Transaction, _atomic_write, canonical_bytes
from .ledger_domain import DOMAIN_SCHEMA
from .ledger_domain import apply as apply_domain_operation
from .ledger_facts import FACT_SCHEMA
from .ledger_facts import apply as apply_fact_operation
from .ledger_workflows import WORKFLOW_SCHEMA
from .ledger_workflows import apply as apply_workflow_operation

REDUCER_VERSION = "1"
PROJECTION_SCHEMA_VERSION = "5"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE projection_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE ledger_transactions (
    transaction_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    digest TEXT NOT NULL,
    ordinal INTEGER NOT NULL UNIQUE
);

CREATE TABLE ledger_parents (
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    parent_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(transaction_id, parent_id),
    UNIQUE(transaction_id, ordinal)
);

CREATE TABLE ledger_operations (
    operation_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    ordinal INTEGER NOT NULL,
    operation_type TEXT NOT NULL,
    operation_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    requires_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(transaction_id, ordinal)
);

CREATE INDEX idx_ledger_operation_type ON ledger_operations(operation_type, transaction_id);
""" + DOMAIN_SCHEMA + FACT_SCHEMA + WORKFLOW_SCHEMA


class LedgerProjector:
    """Build and incrementally maintain projection metadata and operation rows."""

    def __init__(self, repository: LedgerRepository) -> None:
        self.repository = repository
        self.state_dir = repository.root / ".epiq"
        self.path = self.state_dir / "projection.sqlite"
        self.manifest_path = self.state_dir / "projection.json"

    def build(self, *, force: bool = False) -> dict[str, Any]:
        transactions = self.repository.ordered()
        if not force and self.path.exists() and self._can_increment(transactions):
            applied = self._increment(transactions)
            mode = "current" if applied == 0 else "incremental"
        else:
            self._rebuild(transactions)
            applied = len(transactions)
            mode = "rebuild"
        result = self.status()
        result.update({"mode": mode, "applied": applied})
        return result

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "project_id": self.repository.config.project_id,
                "state": "missing",
                "projection": str(self.path),
            }
        with self._connect() as connection:
            meta = {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM projection_meta")
            }
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        return {
            "project_id": meta.get("project_id"),
            "state": "ready" if integrity == "ok" else "invalid",
            "projection": str(self.path),
            "reducer_version": meta.get("reducer_version"),
            "projection_schema_version": meta.get("projection_schema_version"),
            "transactions": int(meta.get("transaction_count", "0")),
            "operations": int(meta.get("operation_count", "0")),
            "frontier": json.loads(meta.get("frontier_json", "[]")),
            "transaction_set_digest": meta.get("transaction_set_digest"),
            "integrity": integrity,
        }

    def verify(self) -> dict[str, Any]:
        expected = self.repository.validate()
        ordered = self.repository.ordered()
        actual = self.status()
        if actual["state"] != "ready":
            raise EpiqError("projection_not_ready", "SQLite projection is missing or invalid")
        comparisons = {
            "project_id": expected["project_id"],
            "transactions": expected["transactions"],
            "operations": expected["operations"],
            "frontier": expected["frontier"],
            "transaction_set_digest": expected["transaction_set_digest"],
            "reducer_version": REDUCER_VERSION,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        }
        mismatches = {
            key: {"expected": value, "actual": actual.get(key)}
            for key, value in comparisons.items()
            if actual.get(key) != value
        }
        if mismatches:
            raise EpiqError(
                "projection_out_of_date",
                f"SQLite projection differs from ledger: {mismatches}",
                "Run epiq build to rebuild the disposable projection.",
            )
        with self._connect() as connection:
            projected_transactions = [
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT transaction_id,digest FROM ledger_transactions ORDER BY ordinal"
                )
            ]
            projected_operations = [
                tuple(row)
                for row in connection.execute(
                    """SELECT operation_id,transaction_id,operation_type,operation_version,
                              payload_json,requires_json,metadata_json
                       FROM ledger_operations ORDER BY transaction_id,ordinal"""
                )
            ]
        expected_transactions = [(item.transaction_id, item.digest) for item in ordered]
        expected_operations = [
                (
                    operation.operation_id,
                    transaction.transaction_id,
                    operation.type,
                    operation.version,
                    canonical_bytes(operation.payload).decode(),
                    canonical_bytes(list(operation.requires)).decode(),
                    canonical_bytes(operation.metadata).decode(),
                )
                for transaction in sorted(ordered, key=lambda item: item.transaction_id)
                for operation in transaction.operations
            ]
        if (
            projected_transactions != expected_transactions
            or projected_operations != expected_operations
        ):
            raise EpiqError(
                "projection_content_mismatch",
                "SQLite projection rows differ from the authoritative ledger",
                "Run epiq build --force to rebuild the disposable projection.",
            )
        return {**actual, "verified": True}

    def semantic_fingerprint(self) -> str:
        """Hash every application table and view independent of SQLite file layout."""
        if not self.path.exists():
            raise EpiqError("projection_not_ready", "SQLite projection is missing")
        objects: list[dict[str, Any]] = []
        with self._connect() as connection:
            definitions = connection.execute(
                """SELECT name,type FROM sqlite_master
                   WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            ).fetchall()
            for definition in definitions:
                name = str(definition["name"])
                columns = [
                    [
                        int(row[0]),
                        str(row[1]),
                        str(row[2]),
                        int(row[3]),
                        row[4],
                        int(row[5]),
                    ]
                    for row in connection.execute(f'PRAGMA table_info("{name}")')
                ]
                rows = [
                    list(row) for row in connection.execute(f'SELECT * FROM "{name}"')
                ]
                rows.sort(key=canonical_bytes)
                objects.append(
                    {
                        "name": name,
                        "type": str(definition["type"]),
                        "columns": columns,
                        "rows": rows,
                    }
                )
        return "sha256:" + hashlib.sha256(canonical_bytes(objects)).hexdigest()

    def dry_run(self, transaction: Transaction) -> None:
        """Validate one candidate against current state, rolling every projection write back."""
        self.build()
        with self._connect() as connection:
            ordinal = int(
                connection.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0]
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._apply(connection, transaction, ordinal)
            finally:
                connection.rollback()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _metadata(self, connection: sqlite3.Connection) -> dict[str, str]:
        try:
            return {
                str(row[0]): str(row[1])
                for row in connection.execute("SELECT key,value FROM projection_meta")
            }
        except sqlite3.DatabaseError:
            return {}

    def _can_increment(self, transactions: list[Transaction]) -> bool:
        try:
            with self._connect() as connection:
                meta = self._metadata(connection)
                if (
                    meta.get("project_id") != self.repository.config.project_id
                    or meta.get("reducer_version") != REDUCER_VERSION
                    or meta.get("projection_schema_version") != PROJECTION_SCHEMA_VERSION
                    or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                ):
                    return False
                applied = [
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT transaction_id,digest FROM ledger_transactions ORDER BY ordinal"
                    )
                ]
        except sqlite3.DatabaseError:
            return False
        expected_prefix = [
            (item.transaction_id, item.digest) for item in transactions[: len(applied)]
        ]
        return applied == expected_prefix

    def _increment(self, transactions: list[Transaction]) -> int:
        with self._connect() as connection:
            current = int(
                connection.execute("SELECT COUNT(*) FROM ledger_transactions").fetchone()[0]
            )
            pending = transactions[current:]
            connection.execute("BEGIN IMMEDIATE")
            for ordinal, transaction in enumerate(pending, start=current):
                self._apply(connection, transaction, ordinal)
            self._write_metadata(connection, transactions)
            connection.commit()
        self._write_manifest()
        return len(pending)

    def _rebuild(self, transactions: list[Transaction]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".projection.", suffix=".sqlite", dir=self.state_dir
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            connection = sqlite3.connect(temporary)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                for ordinal, transaction in enumerate(transactions):
                    self._apply(connection, transaction, ordinal)
                self._write_metadata(connection, transactions)
                connection.commit()
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise EpiqError("invalid_projection", "Rebuilt SQLite projection is invalid")
            finally:
                connection.close()
            os.replace(temporary, self.path)
            directory = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        self._write_manifest()

    def _apply(
        self, connection: sqlite3.Connection, transaction: Transaction, ordinal: int
    ) -> None:
        connection.execute(
            "INSERT INTO ledger_transactions VALUES(?,?,?,?,?,?,?)",
            (
                transaction.transaction_id,
                transaction.project_id,
                transaction.replica_id,
                transaction.actor,
                transaction.recorded_at,
                transaction.digest,
                ordinal,
            ),
        )
        connection.executemany(
            "INSERT INTO ledger_parents VALUES(?,?,?)",
            [
                (transaction.transaction_id, parent, index)
                for index, parent in enumerate(transaction.parents)
            ],
        )
        for operation in transaction.operations:
            apply_domain_operation(
                connection,
                transaction.transaction_id,
                operation.operation_id,
                operation.type,
                operation.payload,
            )
            apply_fact_operation(
                connection,
                transaction.transaction_id,
                operation.operation_id,
                operation.type,
                operation.payload,
            )
            apply_workflow_operation(
                connection,
                transaction.transaction_id,
                operation.operation_id,
                operation.type,
                operation.payload,
            )
        connection.executemany(
            "INSERT INTO ledger_operations VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    operation.operation_id,
                    transaction.transaction_id,
                    index,
                    operation.type,
                    operation.version,
                    canonical_bytes(operation.payload).decode(),
                    canonical_bytes(list(operation.requires)).decode(),
                    canonical_bytes(operation.metadata).decode(),
                )
                for index, operation in enumerate(transaction.operations)
            ],
        )

    def _write_metadata(
        self, connection: sqlite3.Connection, transactions: list[Transaction]
    ) -> None:
        values = {
            "project_id": self.repository.config.project_id,
            "ledger_format_version": self.repository.config.format_version,
            "reducer_version": REDUCER_VERSION,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
            "transaction_count": str(len(transactions)),
            "operation_count": str(sum(len(item.operations) for item in transactions)),
            "frontier_json": canonical_bytes(
                list(self.repository.frontier(transactions))
            ).decode(),
            "transaction_set_digest": self.repository.transaction_set_digest(transactions),
        }
        connection.execute("DELETE FROM projection_meta")
        connection.executemany(
            "INSERT INTO projection_meta(key,value) VALUES(?,?)", values.items()
        )

    def _write_manifest(self) -> None:
        status = self.status()
        manifest = {
            "project_id": status["project_id"],
            "ledger_format_version": self.repository.config.format_version,
            "reducer_version": status["reducer_version"],
            "projection_schema_version": status["projection_schema_version"],
            "transaction_count": status["transactions"],
            "operation_count": status["operations"],
            "frontier": status["frontier"],
            "transaction_set_digest": status["transaction_set_digest"],
        }
        _atomic_write(self.manifest_path, canonical_bytes(manifest) + b"\n")

