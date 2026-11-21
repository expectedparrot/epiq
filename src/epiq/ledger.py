"""Immutable transaction contracts for the Git-native Epiq ledger."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import os
import secrets
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .temporal import instant

SCHEMA_VERSION = "1"
OPERATION_VERSION = 1
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_OPERATION_TYPES = frozenset(
    {
        "project.initialize",
        "entity_kind.define",
        "entity.create",
        "entity.alias",
        "entity.merge",
        "entity.retire",
        "entity.restore",
        "question.define",
        "question.evolve",
        "question.retire",
        "question.restore",
        "question.challenge",
        "question.challenge_resolve",
        "evidence.capture",
        "evidence.assess",
        "claim.assert",
        "claim.support",
        "claim.supersede",
        "claim.retract",
        "claim.validity_end",
        "claim.review",
        "derivation.declare",
        "proposal.create",
        "proposal.review",
        "research.not_found",
        "source.bundle_record",
        "report.record",
    }
)


def _ulid(timestamp_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Return a monotonic-sortable ULID-shaped value without an external dependency."""
    stamp = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    random_bytes = secrets.token_bytes(10) if randomness is None else randomness
    if stamp < 0 or stamp >= 2**48 or len(random_bytes) != 10:
        raise EpiqError("invalid_id_material", "ULID time or randomness is out of range")
    value = int.from_bytes(stamp.to_bytes(6, "big") + random_bytes, "big")
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


def new_id(prefix: str) -> str:
    if prefix not in {"txn", "op"}:
        raise EpiqError("invalid_id_prefix", f"Unsupported ledger ID prefix: {prefix}")
    return f"{prefix}_{_ulid()}"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EpiqError("duplicate_json_key", f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def strict_loads(content: str | bytes) -> Any:
    """Parse strict ledger JSON, rejecting duplicate keys and non-finite numbers."""
    try:
        return json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                EpiqError("invalid_json_number", f"Non-finite JSON number: {value}")
            ),
        )
    except EpiqError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EpiqError("invalid_ledger_json", f"Invalid ledger JSON: {error}") from error


def _validate_json(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EpiqError("invalid_json_number", f"Non-finite number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EpiqError("invalid_json_key", f"JSON key at {path} is not a string")
            _validate_json(item, f"{path}.{key}")
        return
    raise EpiqError(
        "invalid_json_value", f"Unsupported JSON value at {path}: {type(value).__name__}"
    )


def canonical_bytes(document: Any, *, omit_digest: bool = False) -> bytes:
    """Encode Epiq Canonical JSON 1 without a trailing newline."""
    value = dict(document) if isinstance(document, dict) else document
    if omit_digest:
        if not isinstance(value, dict):
            raise EpiqError("invalid_ledger_json", "Only JSON objects can omit a digest")
        value.pop("digest", None)
    _validate_json(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return encoded.encode("utf-8")
    except (UnicodeEncodeError, ValueError) as error:
        raise EpiqError("invalid_ledger_json", f"Cannot encode ledger JSON: {error}") from error


def document_digest(document: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(document, omit_digest=True)).hexdigest()


def _require_keys(document: dict[str, Any], expected: set[str], optional: set[str]) -> None:
    missing = expected - set(document)
    unknown = set(document) - expected - optional
    if missing:
        raise EpiqError("invalid_ledger_document", f"Missing ledger keys: {sorted(missing)}")
    if unknown:
        raise EpiqError("invalid_ledger_document", f"Unknown ledger keys: {sorted(unknown)}")


@dataclass(frozen=True)
class Operation:
    operation_id: str
    type: str
    payload: dict[str, Any]
    version: int = OPERATION_VERSION
    requires: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        type: str,
        payload: dict[str, Any],
        *,
        operation_id: str | None = None,
        requires: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> Operation:
        return cls(
            operation_id or new_id("op"),
            type,
            payload,
            requires=tuple(sorted(requires)),
            metadata=metadata or {},
        )

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> Operation:
        if not isinstance(document, dict):
            raise EpiqError("invalid_ledger_operation", "Ledger operation must be an object")
        _require_keys(
            document,
            {"operation_id", "type", "version", "requires", "payload", "metadata"},
            set(),
        )
        operation = cls(
            str(document["operation_id"]),
            str(document["type"]),
            dict(document["payload"]),
            int(document["version"]),
            tuple(str(item) for item in document.get("requires", [])),
            dict(document.get("metadata", {})),
        )
        operation.validate()
        return operation

    def validate(self) -> None:
        from .ledger_domain import validate_payload
        from .ledger_facts import validate_payload as validate_fact_payload
        from .ledger_workflows import validate_payload as validate_workflow_payload

        if not self.operation_id.startswith("op_") or len(self.operation_id) != 29:
            raise EpiqError("invalid_operation_id", f"Invalid operation ID: {self.operation_id}")
        if self.type not in _OPERATION_TYPES:
            raise EpiqError("unknown_operation_type", f"Unknown operation type: {self.type}")
        if self.version != OPERATION_VERSION:
            raise EpiqError(
                "unsupported_operation_version",
                f"Unsupported {self.type} version: {self.version}",
            )
        if len(self.requires) != len(set(self.requires)):
            raise EpiqError("duplicate_requirement", "Operation requirements must be unique")
        if tuple(sorted(self.requires)) != self.requires:
            raise EpiqError("noncanonical_requirements", "Operation requirements must be sorted")
        _validate_json(self.payload, "$.payload")
        _validate_json(self.metadata, "$.metadata")
        validate_payload(self.type, self.payload)
        validate_fact_payload(self.type, self.payload)
        validate_workflow_payload(self.type, self.payload)

    def as_dict(self) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = {
            "operation_id": self.operation_id,
            "type": self.type,
            "version": self.version,
            "requires": list(self.requires),
            "payload": self.payload,
            "metadata": self.metadata,
        }
        return result


@dataclass(frozen=True)
class Transaction:
    project_id: str
    transaction_id: str
    replica_id: str
    actor: str
    recorded_at: str
    parents: tuple[str, ...]
    operations: tuple[Operation, ...]
    digest: str
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        project_id: str,
        replica_id: str,
        actor: str,
        operations: list[Operation] | tuple[Operation, ...],
        *,
        recorded_at: str,
        parents: list[str] | tuple[str, ...] = (),
        transaction_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Transaction:
        candidate = cls(
            project_id,
            transaction_id or new_id("txn"),
            replica_id,
            actor,
            instant(recorded_at),
            tuple(sorted(parents)),
            tuple(operations),
            "",
            idempotency_key,
            metadata or {},
        )
        document = candidate.as_dict(include_digest=False)
        return cls(**{**candidate.__dict__, "digest": document_digest(document)})

    @classmethod
    def from_dict(cls, document: dict[str, Any], *, verify: bool = True) -> Transaction:
        if not isinstance(document, dict):
            raise EpiqError("invalid_ledger_transaction", "Ledger transaction must be an object")
        _require_keys(
            document,
            {
                "schema_version",
                "project_id",
                "transaction_id",
                "replica_id",
                "actor",
                "recorded_at",
                "parents",
                "operations",
                "metadata",
                "digest",
            },
            {"idempotency_key"},
        )
        if verify:
            expected = document_digest(document)
            supplied = str(document["digest"])
            if not secrets.compare_digest(supplied, expected):
                raise EpiqError(
                    "ledger_digest_mismatch",
                    f"Transaction digest does not match content: {document['transaction_id']}",
                )
        try:
            operations = tuple(Operation.from_dict(item) for item in document["operations"])
            transaction = cls(
                str(document["project_id"]),
                str(document["transaction_id"]),
                str(document["replica_id"]),
                str(document["actor"]),
                str(document["recorded_at"]),
                tuple(str(item) for item in document["parents"]),
                operations,
                str(document["digest"]),
                (
                    str(document["idempotency_key"])
                    if document.get("idempotency_key") is not None
                    else None
                ),
                dict(document.get("metadata", {})),
                str(document["schema_version"]),
            )
        except (TypeError, ValueError) as error:
            raise EpiqError(
                "invalid_ledger_transaction", f"Malformed transaction: {error}"
            ) from error
        transaction.validate(verify=False)
        return transaction

    @classmethod
    def loads(cls, content: str | bytes, *, verify: bool = True) -> Transaction:
        return cls.from_dict(strict_loads(content), verify=verify)

    @classmethod
    def load(cls, path: str | Path, *, verify: bool = True) -> Transaction:
        return cls.loads(Path(path).read_bytes(), verify=verify)

    def validate(self, *, verify: bool = True) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise EpiqError(
                "unsupported_ledger_version", f"Unsupported ledger version: {self.schema_version}"
            )
        if not self.project_id.startswith("prj_"):
            raise EpiqError("invalid_project_id", f"Invalid project ID: {self.project_id}")
        if not self.transaction_id.startswith("txn_") or len(self.transaction_id) != 30:
            raise EpiqError(
                "invalid_transaction_id", f"Invalid transaction ID: {self.transaction_id}"
            )
        if not self.replica_id.startswith("rep_"):
            raise EpiqError("invalid_replica_id", f"Invalid replica ID: {self.replica_id}")
        if not self.actor:
            raise EpiqError("invalid_actor", "Ledger actor cannot be empty")
        if instant(self.recorded_at) != self.recorded_at:
            raise EpiqError("noncanonical_timestamp", "recorded_at must be normalized UTC")
        if not self.operations:
            raise EpiqError("empty_transaction", "Ledger transaction requires an operation")
        if len(self.parents) != len(set(self.parents)):
            raise EpiqError("duplicate_parent", "Transaction parents must be unique")
        if tuple(sorted(self.parents)) != self.parents:
            raise EpiqError("noncanonical_parents", "Transaction parents must be sorted")
        if self.transaction_id in self.parents:
            raise EpiqError("self_parent", "Transaction cannot name itself as a parent")
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise EpiqError("duplicate_operation", "Operation IDs must be unique in a transaction")
        for operation in self.operations:
            operation.validate()
        _validate_json(self.metadata, "$.metadata")
        if verify:
            expected = document_digest(self.as_dict(include_digest=False))
            if not secrets.compare_digest(self.digest, expected):
                raise EpiqError(
                    "ledger_digest_mismatch",
                    f"Transaction digest does not match content: {self.transaction_id}",
                )

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "transaction_id": self.transaction_id,
            "replica_id": self.replica_id,
            "actor": self.actor,
            "recorded_at": self.recorded_at,
            "parents": list(self.parents),
            "operations": [operation.as_dict() for operation in self.operations],
            "metadata": self.metadata,
        }
        if self.idempotency_key is not None:
            result["idempotency_key"] = self.idempotency_key
        if include_digest:
            result["digest"] = self.digest
        return result

    def canonical(self) -> bytes:
        self.validate()
        return canonical_bytes(self.as_dict()) + b"\n"

    def relative_path(self) -> Path:
        token = self.transaction_id.removeprefix("txn_")
        return Path("ledger") / "transactions" / token[:2] / f"{self.transaction_id}.json"


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    name: str
    replica_id: str
    format_version: str = SCHEMA_VERSION

    @classmethod
    def load(cls, path: str | Path) -> ProjectConfig:
        config_path = Path(path)
        try:
            document = tomllib.loads(config_path.read_text())
        except FileNotFoundError as error:
            raise EpiqError(
                "ledger_not_initialized",
                f"Ledger project has no epiq.toml: {config_path.parent}",
            ) from error
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise EpiqError(
                "invalid_project_config", f"Cannot read {config_path}: {error}"
            ) from error
        expected = {"format_version", "project_id", "name", "replica_id"}
        if set(document) != expected:
            raise EpiqError(
                "invalid_project_config",
                f"epiq.toml keys must be exactly {sorted(expected)}",
            )
        config = cls(
            str(document["project_id"]),
            str(document["name"]),
            str(document["replica_id"]),
            str(document["format_version"]),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.format_version != SCHEMA_VERSION:
            raise EpiqError(
                "unsupported_ledger_version",
                f"Unsupported project format: {self.format_version}",
            )
        if not self.project_id.startswith("prj_"):
            raise EpiqError("invalid_project_id", f"Invalid project ID: {self.project_id}")
        if not self.replica_id.startswith("rep_"):
            raise EpiqError("invalid_replica_id", f"Invalid replica ID: {self.replica_id}")
        if not self.name.strip():
            raise EpiqError("invalid_project_name", "Project name cannot be empty")

    def content(self) -> str:
        self.validate()
        return (
            f"format_version = {json.dumps(self.format_version)}\n"
            f"project_id = {json.dumps(self.project_id)}\n"
            f"name = {json.dumps(self.name, ensure_ascii=False)}\n"
            f"replica_id = {json.dumps(self.replica_id)}\n"
        )


class LedgerRepository:
    """A local Git-friendly directory of immutable Epiq transactions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.config = ProjectConfig.load(self.root / "epiq.toml")

    @classmethod
    def initialize(
        cls,
        root: str | Path,
        name: str,
        *,
        actor: str,
        recorded_at: str,
        project_id: str | None = None,
        replica_id: str | None = None,
    ) -> LedgerRepository:
        project_root = Path(root)
        config_path = project_root / "epiq.toml"
        if config_path.exists():
            raise EpiqError("project_exists", f"Ledger project already exists: {project_root}")
        config = ProjectConfig(
            project_id or f"prj_{_ulid()}",
            name,
            replica_id or f"rep_{_ulid()}",
        )
        config.validate()
        project_root.mkdir(parents=True, exist_ok=True)
        (project_root / "ledger" / "transactions").mkdir(parents=True, exist_ok=True)
        schemas = project_root / "ledger" / "schemas"
        schemas.mkdir(parents=True, exist_ok=True)
        package_schemas = Path(__file__).parent / "schemas"
        for filename in ("ledger-operation-1.schema.json", "ledger-transaction-1.schema.json"):
            (schemas / filename).write_bytes((package_schemas / filename).read_bytes())
        _atomic_write(config_path, config.content().encode())
        _ensure_projection_ignored(project_root / ".gitignore")
        repository = cls(project_root)
        genesis = Transaction.create(
            config.project_id,
            config.replica_id,
            actor,
            [
                Operation.create(
                    "project.initialize",
                    {
                        "project_id": config.project_id,
                        "name": config.name,
                        "format_version": config.format_version,
                    },
                )
            ],
            recorded_at=recorded_at,
        )
        repository.append(genesis)
        return repository

    @property
    def transactions_path(self) -> Path:
        return self.root / "ledger" / "transactions"

    def append(self, transaction: Transaction) -> bool:
        """Atomically append a transaction; return False for an identical retry."""
        if transaction.project_id != self.config.project_id:
            raise EpiqError(
                "ledger_project_mismatch",
                f"Transaction belongs to {transaction.project_id}, not {self.config.project_id}",
            )
        transaction.validate()
        known = {item.transaction_id for item in self.transactions()}
        missing = set(transaction.parents) - known
        if missing:
            raise EpiqError("missing_parent", f"Missing transaction parents: {sorted(missing)}")
        destination = self.root / transaction.relative_path()
        if destination.exists():
            existing = Transaction.load(destination)
            if existing == transaction:
                return False
            raise EpiqError(
                "transaction_id_collision",
                f"Transaction ID already has different content: {transaction.transaction_id}",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(destination, transaction.canonical())
        return True

    def transaction_paths(self) -> list[Path]:
        if not self.transactions_path.exists():
            return []
        return sorted(self.transactions_path.glob("*/*.json"))

    def transactions(self) -> list[Transaction]:
        transactions: list[Transaction] = []
        seen: set[str] = set()
        for path in self.transaction_paths():
            transaction = Transaction.load(path)
            expected = self.root / transaction.relative_path()
            if path != expected:
                raise EpiqError(
                    "invalid_transaction_path",
                    f"{transaction.transaction_id} must be stored at {expected}",
                )
            if transaction.transaction_id in seen:
                raise EpiqError(
                    "duplicate_transaction", f"Duplicate transaction: {transaction.transaction_id}"
                )
            seen.add(transaction.transaction_id)
            transactions.append(transaction)
        return transactions

    def ordered(self) -> list[Transaction]:
        """Return stable causal order, rejecting missing parents and cycles."""
        transactions = {item.transaction_id: item for item in self.transactions()}
        children: dict[str, list[str]] = {identifier: [] for identifier in transactions}
        indegree: dict[str, int] = {}
        for identifier, transaction in transactions.items():
            missing = set(transaction.parents) - set(transactions)
            if missing:
                raise EpiqError(
                    "missing_parent", f"{identifier} has missing parents: {sorted(missing)}"
                )
            indegree[identifier] = len(transaction.parents)
            for parent in transaction.parents:
                children[parent].append(identifier)
        ready = [identifier for identifier, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        result: list[Transaction] = []
        while ready:
            identifier = heapq.heappop(ready)
            result.append(transactions[identifier])
            for child in children[identifier]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        if len(result) != len(transactions):
            cyclic = sorted(identifier for identifier, degree in indegree.items() if degree > 0)
            raise EpiqError("causal_cycle", f"Transaction parent cycle: {cyclic}")
        return result

    def frontier(self) -> tuple[str, ...]:
        transactions = self.ordered()
        parents = {parent for transaction in transactions for parent in transaction.parents}
        return tuple(
            sorted(
                item.transaction_id
                for item in transactions
                if item.transaction_id not in parents
            )
        )

    def transaction_set_digest(self) -> str:
        material = "\n".join(
            f"{item.transaction_id} {item.digest}"
            for item in sorted(
                self.transactions(), key=lambda transaction: transaction.transaction_id
            )
        )
        return "sha256:" + hashlib.sha256(material.encode()).hexdigest()

    def validate(self) -> dict[str, Any]:
        transactions = self.ordered()
        for transaction in transactions:
            if transaction.project_id != self.config.project_id:
                raise EpiqError(
                    "ledger_project_mismatch",
                    f"Transaction belongs to {transaction.project_id}: "
                    f"{transaction.transaction_id}",
                )
        return {
            "project_id": self.config.project_id,
            "transactions": len(transactions),
            "operations": sum(len(item.operations) for item in transactions),
            "frontier": list(self.frontier()),
            "transaction_set_digest": self.transaction_set_digest(),
        }


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_projection_ignored(path: Path) -> None:
    existing = path.read_text().splitlines() if path.exists() else []
    if ".epiq/" not in existing:
        content = "\n".join([*existing, ".epiq/"]).lstrip("\n") + "\n"
        _atomic_write(path, content.encode())

