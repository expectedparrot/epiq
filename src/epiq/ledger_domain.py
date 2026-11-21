"""Typed domain validation and SQLite reducers for ledger operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from typing import Any

from .errors import EpiqError

DOMAIN_SCHEMA = """
CREATE TABLE projected_project (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    format_version TEXT NOT NULL,
    created_operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_entity_kinds (
    kind TEXT PRIMARY KEY,
    created_operation_id TEXT NOT NULL UNIQUE
);

CREATE TABLE projected_entities (
    entity_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL REFERENCES projected_entity_kinds(kind),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('entity','observation','relation')),
    identity_json TEXT,
    identity_hash TEXT,
    created_operation_id TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_projected_entity_identity
ON projected_entities(kind,identity_hash);
CREATE INDEX idx_projected_entity_name
ON projected_entities(kind,name_key);

CREATE TABLE projected_entity_aliases (
    entity_id TEXT NOT NULL REFERENCES projected_entities(entity_id),
    alias TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY(entity_id,alias_key)
);

CREATE TABLE projected_entity_merge_edges (
    source_entity_id TEXT NOT NULL REFERENCES projected_entities(entity_id),
    destination_entity_id TEXT NOT NULL REFERENCES projected_entities(entity_id),
    reason TEXT NOT NULL,
    operation_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY(source_entity_id,destination_entity_id)
);

CREATE TABLE projected_entity_visibility_events (
    entity_id TEXT NOT NULL REFERENCES projected_entities(entity_id),
    visible INTEGER NOT NULL CHECK(visible IN (0,1)),
    reason TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT PRIMARY KEY
);

CREATE TABLE projected_questions (
    question_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    subject_kind TEXT NOT NULL REFERENCES projected_entity_kinds(kind),
    value_type TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_operation_id TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_projected_question_name ON projected_questions(name);

CREATE TABLE projected_question_evolution_edges (
    predecessor_question_id TEXT NOT NULL REFERENCES projected_questions(question_id),
    successor_question_id TEXT NOT NULL REFERENCES projected_questions(question_id),
    relationship TEXT NOT NULL CHECK(relationship IN ('replaces','splits','refines')),
    reason TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    PRIMARY KEY(predecessor_question_id,successor_question_id,operation_id)
);

CREATE TABLE projected_question_visibility_events (
    question_id TEXT NOT NULL REFERENCES projected_questions(question_id),
    visible INTEGER NOT NULL CHECK(visible IN (0,1)),
    reason TEXT NOT NULL,
    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
    operation_id TEXT PRIMARY KEY
);

CREATE TABLE projected_conflicts (
    conflict_id TEXT PRIMARY KEY,
    conflict_type TEXT NOT NULL,
    object_ids_json TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX idx_projected_conflict_type ON projected_conflicts(conflict_type,conflict_id);
"""

_SPECS: dict[str, tuple[set[str], set[str]]] = {
    "project.initialize": ({"project_id", "name", "format_version"}, set()),
    "entity_kind.define": ({"kind"}, set()),
    "entity.create": (
        {"entity_id", "kind", "name"},
        {"attributes", "role", "identity"},
    ),
    "entity.alias": ({"entity_id", "alias"}, set()),
    "entity.merge": (
        {"source_entity_id", "destination_entity_id", "reason"},
        set(),
    ),
    "entity.retire": ({"entity_id", "reason"}, set()),
    "entity.restore": ({"entity_id", "reason"}, set()),
    "question.define": (
        {"question_id", "name", "subject_kind", "value_type"},
        {"definition"},
    ),
    "question.evolve": (
        {"predecessor_question_id", "successor_question_ids", "relationship", "reason"},
        set(),
    ),
    "question.retire": ({"question_id", "reason"}, set()),
    "question.restore": ({"question_id", "reason"}, set()),
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EpiqError("invalid_operation_payload", f"{key} must be a non-empty string")
    return value


def validate_payload(operation_type: str, payload: dict[str, Any]) -> None:
    """Validate typed payload structure for operation families already reduced."""
    if operation_type not in _SPECS:
        return
    if not isinstance(payload, dict):
        raise EpiqError("invalid_operation_payload", f"{operation_type} payload must be an object")
    required, optional = _SPECS[operation_type]
    missing = required - set(payload)
    unknown = set(payload) - required - optional
    if missing or unknown:
        raise EpiqError(
            "invalid_operation_payload",
            f"{operation_type} missing={sorted(missing)} unknown={sorted(unknown)}",
        )
    for key in required - {"successor_question_ids"}:
        _required_string(payload, key)
    if operation_type == "entity.create":
        if payload.get("attributes", {}) is not None and not isinstance(
            payload.get("attributes", {}), dict
        ):
            raise EpiqError("invalid_operation_payload", "attributes must be an object")
        if payload.get("identity") is not None and not isinstance(payload["identity"], dict):
            raise EpiqError("invalid_operation_payload", "identity must be an object or null")
        if payload.get("role", "entity") not in {"entity", "observation", "relation"}:
            raise EpiqError("invalid_operation_payload", "Unknown entity role")
    elif operation_type == "question.define":
        if not isinstance(payload.get("definition", {}), dict):
            raise EpiqError("invalid_operation_payload", "definition must be an object")
    elif operation_type == "question.evolve":
        successors = payload["successor_question_ids"]
        if (
            not isinstance(successors, list)
            or not successors
            or not all(isinstance(item, str) and item for item in successors)
            or len(successors) != len(set(successors))
        ):
            raise EpiqError(
                "invalid_operation_payload", "successor_question_ids must be unique strings"
            )
        if payload["relationship"] not in {"replaces", "splits", "refines"}:
            raise EpiqError("invalid_operation_payload", "Unknown question relationship")


def apply(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    operation_type: str,
    payload: dict[str, Any],
) -> None:
    """Apply one typed operation to domain projection tables."""
    validate_payload(operation_type, payload)
    if operation_type == "project.initialize":
        transaction = connection.execute(
            "SELECT project_id FROM ledger_transactions WHERE transaction_id=?", (transaction_id,)
        ).fetchone()
        parent_count = connection.execute(
            "SELECT COUNT(*) FROM ledger_parents WHERE transaction_id=?", (transaction_id,)
        ).fetchone()[0]
        if transaction is None or payload["project_id"] != transaction["project_id"]:
            raise EpiqError(
                "ledger_project_mismatch", "Genesis payload does not match its transaction project"
            )
        if parent_count:
            raise EpiqError("invalid_project_genesis", "Project initialization cannot have parents")
    handler = _HANDLERS.get(operation_type)
    if handler is not None:
        handler(connection, transaction_id, operation_id, payload)


def _require_row(
    connection: sqlite3.Connection, table: str, column: str, identifier: str
) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE {column}=?", (identifier,)
    ).fetchone()
    if row is None:
        raise EpiqError("missing_domain_object", f"Missing {column}: {identifier}")
    return row


def _conflict(
    connection: sqlite3.Connection,
    conflict_type: str,
    object_ids: list[str],
    detail: dict[str, Any],
) -> None:
    identifiers = sorted(set(object_ids))
    material = f"{conflict_type}\n{_json(identifiers)}\n{_json(detail)}"
    conflict_id = "cnf_" + hashlib.sha256(material.encode()).hexdigest()[:24]
    connection.execute(
        "INSERT OR IGNORE INTO projected_conflicts VALUES(?,?,?,?)",
        (conflict_id, conflict_type, _json(identifiers), _json(detail)),
    )


def _resolve_pair_conflict(
    connection: sqlite3.Connection, conflict_type: str, first: str, second: str
) -> None:
    connection.execute(
        "DELETE FROM projected_conflicts WHERE conflict_type=? AND object_ids_json=?",
        (conflict_type, _json(sorted({first, second}))),
    )


def _project_initialize(
    connection: sqlite3.Connection, _transaction_id: str, operation_id: str, payload: dict[str, Any]
) -> None:
    existing = connection.execute("SELECT * FROM projected_project").fetchone()
    if existing is not None:
        raise EpiqError(
            "duplicate_project_initialization", "Project has multiple genesis operations"
        )
    connection.execute(
        "INSERT INTO projected_project VALUES(?,?,?,?)",
        (payload["project_id"], payload["name"], payload["format_version"], operation_id),
    )


def _define_kind(
    connection: sqlite3.Connection, _transaction_id: str, operation_id: str, payload: dict[str, Any]
) -> None:
    existing = connection.execute(
        "SELECT created_operation_id FROM projected_entity_kinds WHERE kind=?", (payload["kind"],)
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO projected_entity_kinds VALUES(?,?)", (payload["kind"], operation_id)
        )


def _create_entity(
    connection: sqlite3.Connection, _transaction_id: str, operation_id: str, payload: dict[str, Any]
) -> None:
    _require_row(connection, "projected_entity_kinds", "kind", payload["kind"])
    identity = payload.get("identity")
    identity_json = _json(identity) if identity is not None else None
    identity_hash = hashlib.sha256(identity_json.encode()).hexdigest() if identity_json else None
    try:
        connection.execute(
            "INSERT INTO projected_entities VALUES(?,?,?,?,?,?,?,?,?)",
            (
                payload["entity_id"],
                payload["kind"],
                payload["name"],
                _key(payload["name"]),
                _json(payload.get("attributes") or {}),
                payload.get("role", "entity"),
                identity_json,
                identity_hash,
                operation_id,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise EpiqError(
            "domain_id_collision", f"Entity ID is already defined: {payload['entity_id']}"
        ) from error
    if identity_hash:
        matches = connection.execute(
            """SELECT entity_id FROM projected_entities
               WHERE kind=? AND identity_hash=? AND entity_id<>?""",
            (payload["kind"], identity_hash, payload["entity_id"]),
        ).fetchall()
        reason = "compound_identity"
    else:
        matches = connection.execute(
            """SELECT entity_id FROM projected_entities
               WHERE kind=? AND name_key=? AND identity_hash IS NULL AND entity_id<>?""",
            (payload["kind"], _key(payload["name"]), payload["entity_id"]),
        ).fetchall()
        reason = "normalized_name"
    for match in matches:
        _conflict(
            connection,
            "entity_identity",
            [str(match[0]), payload["entity_id"]],
            {"kind": payload["kind"], "reason": reason},
        )


def _alias_entity(
    connection: sqlite3.Connection, _transaction_id: str, operation_id: str, payload: dict[str, Any]
) -> None:
    entity = _require_row(connection, "projected_entities", "entity_id", payload["entity_id"])
    alias_key = _key(payload["alias"])
    matches = connection.execute(
        """SELECT entity_id FROM projected_entity_aliases a
           JOIN projected_entities e USING(entity_id)
           WHERE e.kind=? AND a.alias_key=? AND a.entity_id<>?""",
        (entity["kind"], alias_key, payload["entity_id"]),
    ).fetchall()
    connection.execute(
        "INSERT OR IGNORE INTO projected_entity_aliases VALUES(?,?,?,?)",
        (payload["entity_id"], payload["alias"], alias_key, operation_id),
    )
    for match in matches:
        _conflict(
            connection,
            "entity_alias",
            [str(match[0]), payload["entity_id"]],
            {"alias": payload["alias"], "kind": str(entity["kind"])},
        )


def _merge_entity(
    connection: sqlite3.Connection, _transaction_id: str, operation_id: str, payload: dict[str, Any]
) -> None:
    source = _require_row(
        connection, "projected_entities", "entity_id", payload["source_entity_id"]
    )
    destination = _require_row(
        connection, "projected_entities", "entity_id", payload["destination_entity_id"]
    )
    if source["kind"] != destination["kind"]:
        raise EpiqError("entity_kind_mismatch", "Merged entities must have the same kind")
    if source["entity_id"] == destination["entity_id"]:
        raise EpiqError("invalid_entity_merge", "Entity cannot merge into itself")
    connection.execute(
        "INSERT OR IGNORE INTO projected_entity_merge_edges VALUES(?,?,?,?)",
        (
            payload["source_entity_id"],
            payload["destination_entity_id"],
            payload["reason"],
            operation_id,
        ),
    )
    _resolve_pair_conflict(
        connection,
        "entity_identity",
        payload["source_entity_id"],
        payload["destination_entity_id"],
    )
    _resolve_pair_conflict(
        connection,
        "entity_alias",
        payload["source_entity_id"],
        payload["destination_entity_id"],
    )
    outgoing = connection.execute(
        """SELECT destination_entity_id FROM projected_entity_merge_edges
           WHERE source_entity_id=? ORDER BY destination_entity_id""",
        (payload["source_entity_id"],),
    ).fetchall()
    if len(outgoing) > 1:
        _conflict(
            connection,
            "entity_merge",
            [payload["source_entity_id"], *(str(row[0]) for row in outgoing)],
            {"reason": "multiple_destinations"},
        )
    edges: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT source_entity_id,destination_entity_id FROM projected_entity_merge_edges"
    ):
        edges.setdefault(str(row[0]), []).append(str(row[1]))
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(entity_id: str) -> None:
        if entity_id in visiting:
            raise EpiqError("entity_merge_cycle", f"Entity merge cycle includes {entity_id}")
        if entity_id in visited:
            return
        visiting.add(entity_id)
        for destination_id in edges.get(entity_id, []):
            visit(destination_id)
        visiting.remove(entity_id)
        visited.add(entity_id)

    for entity_id in edges:
        visit(entity_id)


def _entity_visibility(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
    *,
    visible: bool,
) -> None:
    _require_row(connection, "projected_entities", "entity_id", payload["entity_id"])
    connection.execute(
        "INSERT INTO projected_entity_visibility_events VALUES(?,?,?,?,?)",
        (payload["entity_id"], int(visible), payload["reason"], transaction_id, operation_id),
    )


def _define_question(
    connection: sqlite3.Connection, _transaction_id: str, operation_id: str, payload: dict[str, Any]
) -> None:
    _require_row(connection, "projected_entity_kinds", "kind", payload["subject_kind"])
    try:
        connection.execute(
            "INSERT INTO projected_questions VALUES(?,?,?,?,?,?)",
            (
                payload["question_id"],
                payload["name"],
                payload["subject_kind"],
                payload["value_type"],
                _json(payload.get("definition") or {}),
                operation_id,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise EpiqError(
            "domain_id_collision", f"Question ID is already defined: {payload['question_id']}"
        ) from error
    matches = connection.execute(
        "SELECT question_id FROM projected_questions WHERE name=? AND question_id<>?",
        (payload["name"], payload["question_id"]),
    ).fetchall()
    for match in matches:
        _conflict(
            connection,
            "question_definition",
            [str(match[0]), payload["question_id"]],
            {"name": payload["name"]},
        )


def _evolve_question(
    connection: sqlite3.Connection, _transaction_id: str, operation_id: str, payload: dict[str, Any]
) -> None:
    predecessor = payload["predecessor_question_id"]
    _require_row(connection, "projected_questions", "question_id", predecessor)
    for successor in payload["successor_question_ids"]:
        _require_row(connection, "projected_questions", "question_id", successor)
        connection.execute(
            "INSERT INTO projected_question_evolution_edges VALUES(?,?,?,?,?)",
            (predecessor, successor, payload["relationship"], payload["reason"], operation_id),
        )
        _resolve_pair_conflict(connection, "question_definition", predecessor, successor)


def _question_visibility(
    connection: sqlite3.Connection,
    transaction_id: str,
    operation_id: str,
    payload: dict[str, Any],
    *,
    visible: bool,
) -> None:
    _require_row(connection, "projected_questions", "question_id", payload["question_id"])
    connection.execute(
        "INSERT INTO projected_question_visibility_events VALUES(?,?,?,?,?)",
        (payload["question_id"], int(visible), payload["reason"], transaction_id, operation_id),
    )


_HANDLERS = {
    "project.initialize": _project_initialize,
    "entity_kind.define": _define_kind,
    "entity.create": _create_entity,
    "entity.alias": _alias_entity,
    "entity.merge": _merge_entity,
    "entity.retire": lambda connection, transaction, operation, payload: _entity_visibility(
        connection, transaction, operation, payload, visible=False
    ),
    "entity.restore": lambda connection, transaction, operation, payload: _entity_visibility(
        connection, transaction, operation, payload, visible=True
    ),
    "question.define": _define_question,
    "question.evolve": _evolve_question,
    "question.retire": lambda connection, transaction, operation, payload: _question_visibility(
        connection, transaction, operation, payload, visible=False
    ),
    "question.restore": lambda connection, transaction, operation, payload: _question_visibility(
        connection, transaction, operation, payload, visible=True
    ),
}

