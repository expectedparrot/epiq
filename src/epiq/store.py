"""Transactional SQLite storage for Epiq's append-only epistemic history."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import EpiqError

QUESTION_CHALLENGE_PROBLEMS = {
    "type_mismatch",
    "cardinality_mismatch",
    "temporal_mismatch",
    "level_mismatch",
    "population_mismatch",
    "predicate_conflation",
    "modal_ambiguity",
    "unit_mismatch",
    "epistemic_mismatch",
    "definition_ambiguity",
    "other",
}

LATEST_SCHEMA_VERSION = 13
MIGRATION_DESCRIPTIONS = {
    2: "multi-evidence claims and derivation lineage",
    3: "source publication time and claim temporal basis",
    4: "question challenges and supporting evidence",
    5: "durable operational agent jobs",
    6: "reversible question visibility",
    7: "entity aliases, merges, and visibility",
    8: "durable claim proposal review queue",
    9: "executable question evolution lineage",
    10: "claim validity endings and evidence assessments",
    11: "first-class evidence source types",
    12: "entity roles, compound identities, and structured source locators",
    13: "typed derivation dependencies and stale-derived-claim detection",
}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'entity' CHECK(role IN ('entity', 'observation', 'relation')),
    identity_json TEXT,
    identity_hash TEXT,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    UNIQUE(kind, name)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_compound_identity
ON entities(kind, identity_hash) WHERE identity_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL UNIQUE,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS entity_redirects (
    from_entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    into_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    reason TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    CHECK(from_entity_id <> into_entity_id)
);

CREATE TABLE IF NOT EXISTS entity_visibility (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    visible INTEGER NOT NULL CHECK(visible IN (0, 1)),
    reason TEXT NOT NULL,
    changed_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS entity_kinds (
    kind TEXT PRIMARY KEY,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS questions (
    question_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    subject_kind TEXT NOT NULL,
    value_type TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS question_visibility (
    name TEXT PRIMARY KEY,
    visible INTEGER NOT NULL CHECK(visible IN (0, 1)),
    reason TEXT NOT NULL,
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    changed_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS question_lineage (
    predecessor_question_id TEXT NOT NULL REFERENCES questions(question_id),
    successor_question_id TEXT NOT NULL REFERENCES questions(question_id),
    relationship TEXT NOT NULL CHECK(relationship IN ('replaces', 'splits', 'refines')),
    reason TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    PRIMARY KEY(predecessor_question_id, successor_question_id)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'web'
        CHECK(source_type IN ('web', 'personal', 'model', 'report', 'interview', 'other')),
    title TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    published_at TEXT,
    content_hash TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    linked_entity_id TEXT REFERENCES entities(entity_id),
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    UNIQUE(url, content_hash)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    excerpt TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS evidence_assessments (
    assessment_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    status TEXT NOT NULL CHECK(status IN ('accepted', 'disputed', 'invalid', 'superseded')),
    reason TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);
CREATE INDEX IF NOT EXISTS idx_evidence_assessments
ON evidence_assessments(evidence_id, created_seq);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES entities(entity_id),
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    value_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    tx_from TEXT NOT NULL,
    tx_to TEXT,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    confidence TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
    temporal_basis TEXT NOT NULL DEFAULT 'observed'
        CHECK(temporal_basis IN ('observed', 'source', 'unknown')),
    status TEXT NOT NULL CHECK(status IN ('asserted', 'superseded', 'retracted')),
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    closed_seq INTEGER REFERENCES events(seq),
    UNIQUE(subject_id, question_id, value_json, valid_from, evidence_id)
);

CREATE TABLE IF NOT EXISTS claim_validity_ends (
    claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id),
    valid_to TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE INDEX IF NOT EXISTS idx_claim_lookup
ON claims(subject_id, question_id, tx_from, tx_to, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    ordinal INTEGER NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    PRIMARY KEY(claim_id, evidence_id),
    UNIQUE(claim_id, ordinal)
);

CREATE TABLE IF NOT EXISTS derivations (
    claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id),
    operation TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS claim_inputs (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    input_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    ordinal INTEGER NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    PRIMARY KEY(claim_id, input_claim_id),
    UNIQUE(claim_id, ordinal),
    CHECK(claim_id <> input_claim_id)
);

CREATE TABLE IF NOT EXISTS derivation_dependencies (
    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    dependency_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    role TEXT NOT NULL CHECK(role IN ('operand', 'parameter', 'path')),
    ordinal INTEGER NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    PRIMARY KEY(claim_id, role, dependency_claim_id),
    UNIQUE(claim_id, role, ordinal),
    CHECK(claim_id <> dependency_claim_id)
);
CREATE INDEX IF NOT EXISTS idx_derivation_dependency_claim
ON derivation_dependencies(dependency_claim_id);

CREATE TABLE IF NOT EXISTS claim_proposals (
    proposal_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES entities(entity_id),
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    value_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
    temporal_basis TEXT NOT NULL CHECK(temporal_basis IN ('observed', 'source', 'unknown')),
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
    resulting_claim_id TEXT REFERENCES claims(claim_id),
    review_reason TEXT,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    reviewed_seq INTEGER REFERENCES events(seq)
);
CREATE INDEX IF NOT EXISTS idx_claim_proposals_status
ON claim_proposals(status, created_seq);
CREATE INDEX IF NOT EXISTS idx_entity_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS research_tasks (
    task_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES entities(entity_id),
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    status TEXT NOT NULL CHECK(status IN ('planned', 'in_progress', 'not_found', 'completed')),
    query TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    closed_seq INTEGER REFERENCES events(seq)
);

CREATE INDEX IF NOT EXISTS idx_task_lookup
ON research_tasks(subject_id, question_id, status, created_seq);

CREATE TABLE IF NOT EXISTS question_challenges (
    challenge_id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    problem TEXT NOT NULL,
    explanation TEXT NOT NULL,
    example_entity_id TEXT REFERENCES entities(entity_id),
    proposed_replacement_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved', 'dismissed')),
    resolution TEXT,
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    closed_seq INTEGER REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS question_challenge_evidence (
    challenge_id TEXT NOT NULL REFERENCES question_challenges(challenge_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(challenge_id, evidence_id),
    UNIQUE(challenge_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_question_challenges
ON question_challenges(question_id, status, created_seq);

CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_jobs_updated ON agent_jobs(updated_at);

CREATE TRIGGER IF NOT EXISTS immutable_events_update
BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_delete
BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_update
BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete
BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_sources_update
BEFORE UPDATE ON sources BEGIN SELECT RAISE(ABORT, 'sources are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_sources_delete
BEFORE DELETE ON sources BEGIN SELECT RAISE(ABORT, 'sources are immutable'); END;
"""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonicalize_url(url: str) -> str:
    """Normalize web locators for evidence identity while preserving semantic parameters."""
    raw = url.strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"}:
        return raw
    if not parts.hostname:
        raise EpiqError("invalid_url", f"Web URL has no host: {url}")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise EpiqError("invalid_url", f"Invalid web URL host: {url}") from error
    if host.startswith("www."):
        host = host[4:]
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError as error:
        raise EpiqError("invalid_url", f"Invalid web URL port: {url}") from error
    netloc = (
        host
        if port is None or (scheme, port) in {("http", 80), ("https", 443)}
        else f"{host}:{port}"
    )
    tracking = {"gclid", "fbclid", "ref"}
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in tracking and not key.casefold().startswith("utm_")
    ]
    return urlunsplit((scheme, netloc, parts.path or "/", urlencode(sorted(query)), ""))


def _normalize_excerpt(excerpt: str) -> str:
    return unicodedata.normalize("NFC", excerpt.replace("\r\n", "\n")).strip()


def _identity_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


class Store:
    """A project-local Epiq database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self, name: str) -> None:
        """Create a database and its immutable project identity."""
        if self.path.exists():
            raise EpiqError("project_exists", f"Database already exists: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("project_id", _id("prj")),
                    ("name", name),
                    ("schema_version", str(LATEST_SCHEMA_VERSION)),
                    ("created_at", _now()),
                ],
            )

    def connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection."""
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone():
            version_row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if version_row is not None and int(version_row["value"]) > LATEST_SCHEMA_VERSION:
                connection.close()
                raise EpiqError(
                    "schema_too_new",
                    f"Database schema v{version_row['value']} is newer than supported "
                    f"v{LATEST_SCHEMA_VERSION}",
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entity_kinds (
                    kind TEXT PRIMARY KEY,
                    created_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                INSERT OR IGNORE INTO entity_kinds(kind,created_seq)
                SELECT kind,MIN(created_seq) FROM entities GROUP BY kind;
                INSERT OR IGNORE INTO entity_kinds(kind,created_seq)
                SELECT subject_kind,MIN(created_seq) FROM questions GROUP BY subject_kind;
                CREATE TABLE IF NOT EXISTS claim_evidence (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
                    ordinal INTEGER NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq),
                    PRIMARY KEY(claim_id, evidence_id),
                    UNIQUE(claim_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS derivations (
                    claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id),
                    operation TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                CREATE TABLE IF NOT EXISTS claim_inputs (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    input_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    ordinal INTEGER NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq),
                    PRIMARY KEY(claim_id, input_claim_id),
                    UNIQUE(claim_id, ordinal),
                    CHECK(claim_id <> input_claim_id)
                );
                INSERT OR IGNORE INTO claim_evidence(claim_id,evidence_id,ordinal,created_seq)
                SELECT claim_id,evidence_id,0,created_seq FROM claims
                WHERE (SELECT CAST(value AS INTEGER) FROM meta WHERE key='schema_version')<2;
                UPDATE meta SET value='2' WHERE key='schema_version' AND CAST(value AS INTEGER)<2;
                """
            )
            source_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(sources)")
            }
            if "published_at" not in source_columns:
                connection.execute("ALTER TABLE sources ADD COLUMN published_at TEXT")
            if "source_type" not in source_columns:
                connection.execute(
                    "ALTER TABLE sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'web'"
                )
            claim_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(claims)")
            }
            if "temporal_basis" not in claim_columns:
                connection.execute(
                    "ALTER TABLE claims ADD COLUMN temporal_basis TEXT NOT NULL DEFAULT 'unknown'"
                )
            connection.execute(
                "UPDATE meta SET value='3' WHERE key='schema_version' AND CAST(value AS INTEGER)<3"
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS question_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    question_id TEXT NOT NULL REFERENCES questions(question_id),
                    problem TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    example_entity_id TEXT REFERENCES entities(entity_id),
                    proposed_replacement_json TEXT,
                    status TEXT NOT NULL CHECK(status IN ('open', 'resolved', 'dismissed')),
                    resolution TEXT,
                    created_seq INTEGER NOT NULL REFERENCES events(seq),
                    closed_seq INTEGER REFERENCES events(seq)
                );
                CREATE TABLE IF NOT EXISTS question_challenge_evidence (
                    challenge_id TEXT NOT NULL REFERENCES question_challenges(challenge_id),
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(challenge_id, evidence_id),
                    UNIQUE(challenge_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_question_challenges
                ON question_challenges(question_id, status, created_seq);
                UPDATE meta SET value='4'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<4;
                CREATE TABLE IF NOT EXISTS agent_jobs (
                    job_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_jobs_updated ON agent_jobs(updated_at);
                UPDATE meta SET value='5'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<5;
                CREATE TABLE IF NOT EXISTS question_visibility (
                    name TEXT PRIMARY KEY,
                    visible INTEGER NOT NULL CHECK(visible IN (0, 1)),
                    reason TEXT NOT NULL,
                    question_id TEXT NOT NULL REFERENCES questions(question_id),
                    changed_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                UPDATE meta SET value='6'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<6;
                CREATE TABLE IF NOT EXISTS entity_aliases (
                    alias_id TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL UNIQUE,
                    created_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                CREATE TABLE IF NOT EXISTS entity_redirects (
                    from_entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
                    into_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    reason TEXT NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq),
                    CHECK(from_entity_id <> into_entity_id)
                );
                CREATE TABLE IF NOT EXISTS entity_visibility (
                    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
                    visible INTEGER NOT NULL CHECK(visible IN (0, 1)),
                    reason TEXT NOT NULL,
                    changed_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                UPDATE meta SET value='7'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<7;
                CREATE TABLE IF NOT EXISTS claim_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL REFERENCES entities(entity_id),
                    question_id TEXT NOT NULL REFERENCES questions(question_id),
                    value_json TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    confidence TEXT NOT NULL CHECK(confidence IN ('low', 'medium', 'high')),
                    temporal_basis TEXT NOT NULL
                        CHECK(temporal_basis IN ('observed', 'source', 'unknown')),
                    rationale TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                    resulting_claim_id TEXT REFERENCES claims(claim_id),
                    review_reason TEXT,
                    created_seq INTEGER NOT NULL REFERENCES events(seq),
                    reviewed_seq INTEGER REFERENCES events(seq)
                );
                CREATE INDEX IF NOT EXISTS idx_claim_proposals_status
                ON claim_proposals(status, created_seq);
                UPDATE meta SET value='8'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<8;
                CREATE TABLE IF NOT EXISTS question_lineage (
                    predecessor_question_id TEXT NOT NULL REFERENCES questions(question_id),
                    successor_question_id TEXT NOT NULL REFERENCES questions(question_id),
                    relationship TEXT NOT NULL
                        CHECK(relationship IN ('replaces', 'splits', 'refines')),
                    reason TEXT NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq),
                    PRIMARY KEY(predecessor_question_id, successor_question_id)
                );
                UPDATE meta SET value='9'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<9;
                CREATE TABLE IF NOT EXISTS evidence_assessments (
                    assessment_id TEXT PRIMARY KEY,
                    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
                    status TEXT NOT NULL
                        CHECK(status IN ('accepted', 'disputed', 'invalid', 'superseded')),
                    reason TEXT NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_assessments
                ON evidence_assessments(evidence_id, created_seq);
                CREATE TABLE IF NOT EXISTS claim_validity_ends (
                    claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id),
                    valid_to TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                UPDATE meta SET value='10'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<10;
                CREATE TRIGGER IF NOT EXISTS immutable_events_update
                BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_events_delete
                BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_evidence_update
                BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete
                BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_sources_update
                BEFORE UPDATE ON sources BEGIN SELECT RAISE(ABORT, 'sources are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_sources_delete
                BEFORE DELETE ON sources BEGIN SELECT RAISE(ABORT, 'sources are immutable'); END;
                UPDATE meta SET value='11'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<11;
                """
            )
            entity_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(entities)")
            }
            if "role" not in entity_columns:
                connection.execute(
                    "ALTER TABLE entities ADD COLUMN role TEXT NOT NULL DEFAULT 'entity'"
                )
            if "identity_json" not in entity_columns:
                connection.execute("ALTER TABLE entities ADD COLUMN identity_json TEXT")
            if "identity_hash" not in entity_columns:
                connection.execute("ALTER TABLE entities ADD COLUMN identity_hash TEXT")
            source_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(sources)")
            }
            if "locator_json" not in source_columns:
                connection.execute(
                    "ALTER TABLE sources ADD COLUMN locator_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "linked_entity_id" not in source_columns:
                connection.execute("ALTER TABLE sources ADD COLUMN linked_entity_id TEXT")
            connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_compound_identity
                ON entities(kind, identity_hash) WHERE identity_hash IS NOT NULL;
                UPDATE meta SET value='12'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<12;
                CREATE TABLE IF NOT EXISTS derivation_dependencies (
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    dependency_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    role TEXT NOT NULL CHECK(role IN ('operand', 'parameter', 'path')),
                    ordinal INTEGER NOT NULL,
                    created_seq INTEGER NOT NULL REFERENCES events(seq),
                    PRIMARY KEY(claim_id, role, dependency_claim_id),
                    UNIQUE(claim_id, role, ordinal),
                    CHECK(claim_id <> dependency_claim_id)
                );
                CREATE INDEX IF NOT EXISTS idx_derivation_dependency_claim
                ON derivation_dependencies(dependency_claim_id);
                INSERT OR IGNORE INTO derivation_dependencies
                    (claim_id,dependency_claim_id,role,ordinal,created_seq)
                SELECT claim_id,input_claim_id,'operand',ordinal,created_seq FROM claim_inputs;
                UPDATE meta SET value='13'
                WHERE key='schema_version' AND CAST(value AS INTEGER)<13;
                """
            )
            connection.commit()
        return connection

    def migration_plan(self) -> dict[str, Any]:
        """Inspect pending database migrations without applying them."""
        if not self.path.exists():
            raise EpiqError("project_not_found", f"Database does not exist: {self.path}")
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            raise EpiqError("invalid_project", "Database has no schema_version metadata")
        current = int(row[0])
        if current > LATEST_SCHEMA_VERSION:
            raise EpiqError(
                "schema_too_new",
                f"Database schema v{current} is newer than supported v{LATEST_SCHEMA_VERSION}",
            )
        pending = [
            {"version": version, "description": MIGRATION_DESCRIPTIONS[version]}
            for version in range(current + 1, LATEST_SCHEMA_VERSION + 1)
        ]
        return {
            "database": str(self.path.resolve()),
            "current_version": current,
            "target_version": LATEST_SCHEMA_VERSION,
            "pending": pending,
            "migration_required": bool(pending),
        }

    def migrate(self, backup: str | Path | None = None) -> dict[str, Any]:
        """Apply pending migrations, optionally taking a raw pre-migration SQLite backup."""
        before = self.migration_plan()
        backup_path = None
        if before["migration_required"] and backup is not None:
            backup_path = Path(backup).expanduser().resolve()
            if backup_path.exists():
                raise EpiqError("backup_exists", f"Backup already exists: {backup_path}")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as source, sqlite3.connect(backup_path) as target:
                source.backup(target)
        with self.connect():
            pass
        after = self.migration_plan()
        return {
            "before": before,
            "after": after,
            "backup": str(backup_path) if backup_path else None,
        }

    def save_agent_job(self, job: dict[str, Any]) -> None:
        """Persist replaceable execution state; research facts remain event-sourced."""
        job_id = str(job.get("job_id", ""))
        created_at = str(job.get("created_at", ""))
        if not job_id or not created_at:
            raise EpiqError("invalid_agent_job", "Agent jobs require job_id and created_at")
        updated_at = _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO agent_jobs(job_id,payload_json,created_at,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     payload_json=excluded.payload_json,
                     updated_at=excluded.updated_at""",
                (job_id, _json(job), created_at, updated_at),
            )
            connection.commit()

    def agent_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return recent operational agent jobs, newest first."""
        if limit < 1 or limit > 1000:
            raise EpiqError("invalid_limit", "Agent job limit must be between 1 and 1000")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT payload_json FROM agent_jobs
                   ORDER BY created_at DESC, job_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def doctor(self) -> dict[str, Any]:
        """Run SQLite and event/materialization consistency checks without changing facts."""
        findings: list[dict[str, str]] = []
        with self.connect() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                findings.append({"code": "sqlite_integrity", "message": integrity})
            for row in connection.execute("PRAGMA foreign_key_check").fetchall():
                findings.append(
                    {
                        "code": "foreign_key_violation",
                        "message": f"{row[0]} row {row[1]} references {row[2]}",
                    }
                )
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "events",
                    "entities",
                    "entity_aliases",
                    "entity_redirects",
                    "entity_visibility",
                    "questions",
                    "question_lineage",
                    "question_visibility",
                    "sources",
                    "evidence",
                    "evidence_assessments",
                    "claims",
                    "claim_validity_ends",
                    "claim_proposals",
                    "research_tasks",
                    "question_challenges",
                    "agent_jobs",
                )
            }
            bad_payloads = 0
            for row in connection.execute("SELECT seq,payload_json FROM events"):
                try:
                    payload = json.loads(str(row["payload_json"]))
                    if not isinstance(payload, dict):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    bad_payloads += 1
                    findings.append(
                        {
                            "code": "invalid_event_payload",
                            "message": f"Event sequence {row['seq']} is not a JSON object",
                        }
                    )
            project = {
                str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key,value FROM meta")
            }
            schema_version = int(project.get("schema_version", "0"))
            if schema_version != LATEST_SCHEMA_VERSION:
                findings.append(
                    {
                        "code": "schema_version_mismatch",
                        "message": (
                            f"Schema is v{schema_version}; expected v{LATEST_SCHEMA_VERSION}"
                        ),
                    }
                )
            materializations = {
                "entities": {"entity.create"},
                "questions": {"question.define"},
                "evidence": {"evidence.add"},
                "claims": {"claim.assert", "claim.derive"},
            }
            for table, expected_types in materializations.items():
                placeholders = ",".join("?" for _ in expected_types)
                bad = connection.execute(
                    f"""SELECT COUNT(*) FROM {table} item JOIN events ev ON ev.seq=item.created_seq
                        WHERE ev.event_type NOT IN ({placeholders})""",
                    tuple(expected_types),
                ).fetchone()[0]
                if bad:
                    findings.append(
                        {
                            "code": "materialization_event_mismatch",
                            "message": f"{table} has {bad} rows backed by the wrong event type",
                        }
                    )
            missing_primary_links = connection.execute(
                """SELECT COUNT(*) FROM claims c WHERE NOT EXISTS (
                     SELECT 1 FROM claim_evidence ce
                     WHERE ce.claim_id=c.claim_id AND ce.evidence_id=c.evidence_id AND ce.ordinal=0
                   )"""
            ).fetchone()[0]
            if missing_primary_links:
                findings.append(
                    {
                        "code": "claim_evidence_projection_mismatch",
                        "message": (
                            f"{missing_primary_links} claims lack their primary evidence link"
                        ),
                    }
                )
        return {
            "ok": not findings,
            "database": str(self.path.resolve()),
            "project": project,
            "sqlite_integrity": integrity,
            "counts": counts,
            "checked_event_payloads": counts["events"] - bad_payloads,
            "findings": findings,
        }

    def backup(self, destination: str | Path, overwrite: bool = False) -> Path:
        """Create a transactionally consistent standalone SQLite backup."""
        output = Path(destination).expanduser().resolve()
        if output == self.path.expanduser().resolve():
            raise EpiqError("invalid_backup", "Backup destination must differ from the database")
        if output.exists() and not overwrite:
            raise EpiqError(
                "backup_exists",
                f"Backup already exists: {output}",
                "Choose another path or pass --force.",
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with self.connect() as source, sqlite3.connect(temporary) as target:
                source.backup(target)
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return output

    def export_bundle(self, destination: str | Path, overwrite: bool = False) -> Path:
        """Export a portable database plus a checksummed manifest in a deterministic ZIP."""
        output = Path(destination).expanduser().resolve()
        if output.exists() and not overwrite:
            raise EpiqError("bundle_exists", f"Bundle already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="epiq-bundle-") as directory:
            database = Path(directory) / "project.sqlite"
            self.backup(database)
            database_bytes = database.read_bytes()
            overview = self.overview()
            manifest = {
                "format": "epiq-project-bundle",
                "version": 1,
                "project": overview["project"],
                "files": {
                    "project.sqlite": {
                        "sha256": hashlib.sha256(database_bytes).hexdigest(),
                        "bytes": len(database_bytes),
                    }
                },
            }
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
                    archive.writestr("project.sqlite", database_bytes)
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        return output

    @classmethod
    def import_bundle(cls, bundle: str | Path, destination: str | Path) -> Store:
        """Verify and install a portable bundle at a new database path."""
        source = Path(bundle).expanduser().resolve()
        output = Path(destination).expanduser().resolve()
        if output.exists():
            raise EpiqError("project_exists", f"Database already exists: {output}")
        try:
            with zipfile.ZipFile(source) as archive:
                names = set(archive.namelist())
                if names != {"manifest.json", "project.sqlite"}:
                    raise EpiqError("invalid_bundle", "Bundle contains unexpected or missing files")
                manifest = json.loads(archive.read("manifest.json"))
                database_bytes = archive.read("project.sqlite")
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
            raise EpiqError("invalid_bundle", f"Cannot read Epiq bundle: {error}") from error
        if manifest.get("format") != "epiq-project-bundle" or manifest.get("version") != 1:
            raise EpiqError("invalid_bundle", "Unsupported Epiq bundle format or version")
        expected = manifest.get("files", {}).get("project.sqlite", {})
        digest = hashlib.sha256(database_bytes).hexdigest()
        if expected.get("sha256") != digest or expected.get("bytes") != len(database_bytes):
            raise EpiqError(
                "bundle_checksum_mismatch", "project.sqlite checksum does not match manifest"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_bytes(database_bytes)
            with sqlite3.connect(temporary) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise EpiqError("bundle_integrity_error", integrity)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return cls(output)

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search identities, schema, evidence, and claim values with stable result shapes."""
        needle = query.strip()
        if not needle:
            raise EpiqError("invalid_search", "Search text cannot be empty")
        if limit < 1 or limit > 500:
            raise EpiqError("invalid_limit", "Search limit must be between 1 and 500")
        sql = """
            SELECT 'entity' record_type,entity_id record_id,name title,
                   attributes_json text,created_seq
            FROM entities WHERE instr(lower(name || ' ' || attributes_json),lower(?))>0
            UNION ALL
            SELECT 'question',question_id,name,definition_json,created_seq
            FROM questions WHERE instr(lower(name || ' ' || definition_json),lower(?))>0
            UNION ALL
            SELECT 'source',source_id,title,url,created_seq
            FROM sources WHERE instr(lower(title || ' ' || url),lower(?))>0
            UNION ALL
            SELECT 'evidence',e.evidence_id,s.title,e.excerpt,e.created_seq
            FROM evidence e JOIN sources s ON s.source_id=e.source_id
            WHERE instr(lower(s.title || ' ' || e.excerpt),lower(?))>0
            UNION ALL
            SELECT 'claim',c.claim_id,e.name || ' · ' || q.name,c.value_json,c.created_seq
            FROM claims c JOIN entities e ON e.entity_id=c.subject_id
                          JOIN questions q ON q.question_id=c.question_id
            WHERE instr(lower(e.name || ' ' || q.name || ' ' || c.value_json),lower(?))>0
            ORDER BY created_seq DESC LIMIT ?
        """
        with self.connect() as connection:
            rows = connection.execute(
                sql, (needle, needle, needle, needle, needle, limit)
            ).fetchall()
        return [
            {
                "record_type": str(row["record_type"]),
                "record_id": str(row["record_id"]),
                "title": str(row["title"]),
                "text": str(row["text"])[:1000],
                "created_seq": int(row["created_seq"]),
            }
            for row in rows
        ]

    def add_entity_kind(self, kind: str, actor: str) -> str:
        """Define an empty row type so a sheet can exist before its first entity."""
        normalized = kind.strip()
        if not normalized:
            raise EpiqError("invalid_entity_kind", "Row type cannot be empty")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT kind FROM entity_kinds WHERE kind=?", (normalized,)
            ).fetchone()
            if existing:
                return str(existing["kind"])
            seq, _, _ = self._event(connection, "entity_kind.define", actor, {"kind": normalized})
            connection.execute("INSERT INTO entity_kinds VALUES(?,?)", (normalized, seq))
        return normalized

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open an immediate transaction, serializing concurrent writers."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _event(
        self, connection: sqlite3.Connection, event_type: str, actor: str, payload: dict[str, Any]
    ) -> tuple[int, str, str]:
        event_id, recorded_at = _id("evt"), _now()
        cursor = connection.execute(
            """INSERT INTO events(event_id,event_type,recorded_at,actor,payload_json)
               VALUES(?,?,?,?,?)""",
            (event_id, event_type, recorded_at, actor, _json(payload)),
        )
        return int(cursor.lastrowid), event_id, recorded_at

    def add_entity(
        self,
        kind: str,
        name: str,
        attributes: dict[str, Any] | None,
        actor: str,
        role: str = "entity",
        identity: dict[str, Any] | None = None,
    ) -> str:
        """Add an entity through an event."""
        try:
            with self.transaction() as connection:
                return self._add_entity_tx(
                    connection, kind, name, attributes, actor, role, identity
                )
        except sqlite3.IntegrityError as exc:
            raise EpiqError("duplicate_entity", f"Entity already exists: {kind} {name}") from exc

    def _add_entity_tx(
        self,
        connection: sqlite3.Connection,
        kind: str,
        name: str,
        attributes: dict[str, Any] | None,
        actor: str,
        role: str = "entity",
        identity: dict[str, Any] | None = None,
    ) -> str:
        if role not in {"entity", "observation", "relation"}:
            raise EpiqError("invalid_entity_role", f"Unknown entity role: {role}")
        normalized_identity = _json(identity) if identity is not None else None
        identity_hash = (
            hashlib.sha256(normalized_identity.encode()).hexdigest()
            if normalized_identity is not None
            else None
        )
        if identity_hash is not None:
            existing = connection.execute(
                "SELECT entity_id FROM entities WHERE kind=? AND identity_hash=?",
                (kind, identity_hash),
            ).fetchone()
            if existing:
                return str(existing["entity_id"])
        entity_id = _id("ent")
        payload = {
            "entity_id": entity_id,
            "kind": kind,
            "name": name,
            "attributes": attributes or {},
            "role": role,
            "identity": identity,
        }
        duplicate = self._find_entity_by_identity(connection, name, kind)
        if duplicate is not None:
            raise EpiqError(
                "duplicate_entity",
                f"Entity already exists: {duplicate['name']} ({duplicate['entity_id']})",
            )
        seq, _, _ = self._event(connection, "entity.create", actor, payload)
        connection.execute("INSERT OR IGNORE INTO entity_kinds VALUES(?,?)", (kind, seq))
        connection.execute(
            """INSERT INTO entities(
                 entity_id,kind,name,attributes_json,role,identity_json,identity_hash,created_seq
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                entity_id,
                kind,
                name,
                _json(attributes or {}),
                role,
                normalized_identity,
                identity_hash,
                seq,
            ),
        )
        return entity_id

    def _find_entity_by_identity(
        self, connection: sqlite3.Connection, reference: str, kind: str | None = None
    ) -> sqlite3.Row | None:
        key = _identity_key(reference)
        rows = connection.execute(
            "SELECT * FROM entities" + (" WHERE kind=?" if kind else ""),
            (kind,) if kind else (),
        ).fetchall()
        direct = [row for row in rows if _identity_key(str(row["name"])) == key]
        aliases = connection.execute(
            """SELECT e.* FROM entity_aliases a JOIN entities e ON e.entity_id=a.entity_id
               WHERE a.normalized_alias=?"""
            + (" AND e.kind=?" if kind else ""),
            (key, kind) if kind else (key,),
        ).fetchall()
        candidates = {str(row["entity_id"]): row for row in [*direct, *aliases]}
        if len(candidates) > 1:
            raise EpiqError(
                "entity_ambiguous",
                f"Entity reference is ambiguous: {reference}",
                "Use an exact entity ID.",
            )
        return next(iter(candidates.values()), None)

    def add_entity_alias(self, entity: str, alias: str, actor: str) -> str:
        """Attach a normalized alternate identity without rewriting the entity."""
        normalized = _identity_key(alias)
        if not normalized:
            raise EpiqError("invalid_alias", "Alias cannot be empty")
        with self.transaction() as connection:
            target = self._resolve_entity(connection, entity)
            existing = self._find_entity_by_identity(connection, alias)
            if existing is not None:
                existing = self._follow_entity_redirect(connection, existing)
                if existing["entity_id"] == target["entity_id"]:
                    return str(target["entity_id"])
                raise EpiqError(
                    "alias_conflict",
                    f"Alias already identifies {existing['name']}: {alias}",
                )
            alias_id = _id("als")
            seq, _, _ = self._event(
                connection,
                "entity.alias",
                actor,
                {
                    "alias_id": alias_id,
                    "entity_id": str(target["entity_id"]),
                    "alias": alias.strip(),
                },
            )
            connection.execute(
                "INSERT INTO entity_aliases VALUES(?,?,?,?,?)",
                (alias_id, str(target["entity_id"]), alias.strip(), normalized, seq),
            )
        return alias_id

    def merge_entities(self, source: str, destination: str, reason: str, actor: str) -> str:
        """Redirect one entity identity into another without rewriting historical claims."""
        if not reason.strip():
            raise EpiqError("reason_required", "A merge reason is required")
        with self.transaction() as connection:
            source_row = self._resolve_entity(connection, source)
            destination_row = self._resolve_entity(connection, destination)
            if source_row["entity_id"] == destination_row["entity_id"]:
                raise EpiqError("merge_same_entity", "An entity cannot be merged into itself")
            if source_row["kind"] != destination_row["kind"]:
                raise EpiqError(
                    "entity_kind_mismatch",
                    f"Cannot merge {source_row['kind']} into {destination_row['kind']}",
                )
            seq, _, _ = self._event(
                connection,
                "entity.merge",
                actor,
                {
                    "from_entity_id": str(source_row["entity_id"]),
                    "into_entity_id": str(destination_row["entity_id"]),
                    "reason": reason.strip(),
                },
            )
            connection.execute(
                "INSERT INTO entity_redirects VALUES(?,?,?,?)",
                (
                    str(source_row["entity_id"]),
                    str(destination_row["entity_id"]),
                    reason.strip(),
                    seq,
                ),
            )
        return str(destination_row["entity_id"])

    def set_entity_visibility(self, reference: str, visible: bool, reason: str, actor: str) -> str:
        """Retire or restore a row while preserving identity and claims."""
        if not reason.strip():
            raise EpiqError("reason_required", "A reason is required")
        with self.transaction() as connection:
            entity = self._resolve_entity(connection, reference)
            current = connection.execute(
                "SELECT visible FROM entity_visibility WHERE entity_id=?",
                (entity["entity_id"],),
            ).fetchone()
            currently_visible = current is None or bool(current["visible"])
            if currently_visible == visible:
                state = "active" if visible else "retired"
                raise EpiqError("entity_visibility_unchanged", f"Entity is already {state}")
            event_type = "entity.restore" if visible else "entity.retire"
            seq, _, _ = self._event(
                connection,
                event_type,
                actor,
                {
                    "entity_id": str(entity["entity_id"]),
                    "reason": reason.strip(),
                },
            )
            connection.execute(
                """INSERT INTO entity_visibility VALUES(?,?,?,?)
                   ON CONFLICT(entity_id) DO UPDATE SET visible=excluded.visible,
                     reason=excluded.reason,changed_seq=excluded.changed_seq""",
                (str(entity["entity_id"]), int(visible), reason.strip(), seq),
            )
        return str(entity["entity_id"])

    def add_question(
        self,
        name: str,
        subject_kind: str,
        value_type: str,
        definition: dict[str, Any],
        actor: str,
    ) -> str:
        """Add the next immutable version of a typed question."""
        self._check_type_declaration(value_type)
        with self.transaction() as connection:
            return self._add_question_tx(
                connection, name, subject_kind, value_type, definition, actor
            )[0]

    def _add_question_tx(
        self,
        connection: sqlite3.Connection,
        name: str,
        subject_kind: str,
        value_type: str,
        definition: dict[str, Any],
        actor: str,
    ) -> tuple[str, int]:
        self._check_type_declaration(value_type)
        if not name.strip():
            raise EpiqError("invalid_question_name", "Question name cannot be empty")
        row = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS version FROM questions WHERE name=?", (name,)
        ).fetchone()
        version = int(row["version"])
        question_id = f"q_{name}_v{version}"
        payload = {
            "question_id": question_id,
            "name": name,
            "version": version,
            "subject_kind": subject_kind,
            "value_type": value_type,
            "definition": definition,
        }
        seq, _, _ = self._event(connection, "question.define", actor, payload)
        connection.execute("INSERT OR IGNORE INTO entity_kinds VALUES(?,?)", (subject_kind, seq))
        connection.execute(
            "INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
            (question_id, name, version, subject_kind, value_type, _json(definition), seq),
        )
        return question_id, seq

    def evolve_question(
        self,
        predecessor: str,
        replacements: list[dict[str, Any]],
        relationship: str,
        reason: str,
        actor: str,
        retire_predecessor: bool = True,
    ) -> list[str]:
        """Atomically replace, refine, or split a field with explicit schema lineage."""
        if relationship not in {"replaces", "splits", "refines"}:
            raise EpiqError("invalid_schema_relationship", f"Unknown relationship: {relationship}")
        if not reason.strip():
            raise EpiqError("reason_required", "A schema-evolution reason is required")
        if not replacements:
            raise EpiqError("replacement_required", "At least one successor question is required")
        if relationship != "splits" and len(replacements) != 1:
            raise EpiqError(
                "invalid_schema_evolution",
                f"{relationship} requires exactly one successor; use splits for several",
            )
        successor_ids: list[str] = []
        with self.transaction() as connection:
            old = self._resolve_question(connection, predecessor)
            for index, replacement in enumerate(replacements):
                try:
                    subject_kind = str(replacement.get("subject_kind", old["subject_kind"]))
                    if subject_kind != old["subject_kind"]:
                        raise EpiqError(
                            "subject_type_mismatch",
                            "Schema evolution cannot change the field's subject entity kind",
                        )
                    question_id, _ = self._add_question_tx(
                        connection,
                        str(replacement["name"]),
                        subject_kind,
                        str(replacement["value_type"]),
                        dict(replacement.get("definition", {})),
                        actor,
                    )
                except KeyError as error:
                    raise EpiqError(
                        "invalid_replacement",
                        f"Replacement {index} is missing {error.args[0]}",
                    ) from error
                successor_ids.append(question_id)
            seq, _, _ = self._event(
                connection,
                "question.evolve",
                actor,
                {
                    "predecessor_question_id": str(old["question_id"]),
                    "successor_question_ids": successor_ids,
                    "relationship": relationship,
                    "reason": reason.strip(),
                    "retired_predecessor": retire_predecessor,
                },
            )
            connection.executemany(
                "INSERT INTO question_lineage VALUES(?,?,?,?,?)",
                [
                    (str(old["question_id"]), successor, relationship, reason.strip(), seq)
                    for successor in successor_ids
                ],
            )
            successor_names = {str(item["name"]) for item in replacements}
            if retire_predecessor and str(old["name"]) not in successor_names:
                connection.execute(
                    """INSERT INTO question_visibility VALUES(?,?,?,?,?)
                       ON CONFLICT(name) DO UPDATE SET visible=excluded.visible,
                         reason=excluded.reason,question_id=excluded.question_id,
                         changed_seq=excluded.changed_seq""",
                    (str(old["name"]), 0, reason.strip(), str(old["question_id"]), seq),
                )
        return successor_ids

    def question_lineage(self, reference: str) -> dict[str, Any]:
        """Describe incoming and outgoing schema-evolution edges for a field version."""
        with self.connect() as connection:
            question = self._resolve_question(connection, reference)
            outgoing = connection.execute(
                """SELECT l.*,q.name successor_name FROM question_lineage l
                   JOIN questions q ON q.question_id=l.successor_question_id
                   WHERE l.predecessor_question_id=? ORDER BY l.created_seq""",
                (question["question_id"],),
            ).fetchall()
            incoming = connection.execute(
                """SELECT l.*,q.name predecessor_name FROM question_lineage l
                   JOIN questions q ON q.question_id=l.predecessor_question_id
                   WHERE l.successor_question_id=? ORDER BY l.created_seq""",
                (question["question_id"],),
            ).fetchall()
        return {
            "question_id": str(question["question_id"]),
            "name": str(question["name"]),
            "predecessors": [
                {
                    "question_id": str(row["predecessor_question_id"]),
                    "name": str(row["predecessor_name"]),
                    "relationship": str(row["relationship"]),
                    "reason": str(row["reason"]),
                }
                for row in incoming
            ],
            "successors": [
                {
                    "question_id": str(row["successor_question_id"]),
                    "name": str(row["successor_name"]),
                    "relationship": str(row["relationship"]),
                    "reason": str(row["reason"]),
                }
                for row in outgoing
            ],
        }

    def set_question_visibility(
        self, reference: str, visible: bool, reason: str, actor: str
    ) -> str:
        """Retire or restore a field through an append-only visibility event."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise EpiqError("reason_required", "A reason is required")
        with self.transaction() as connection:
            question = self._resolve_question(connection, reference)
            current = connection.execute(
                "SELECT visible FROM question_visibility WHERE name=?", (question["name"],)
            ).fetchone()
            currently_visible = current is None or bool(current["visible"])
            if currently_visible == visible:
                state = "active" if visible else "retired"
                raise EpiqError("question_visibility_unchanged", f"Field is already {state}")
            event_type = "question.restore" if visible else "question.retire"
            seq, _, _ = self._event(
                connection,
                event_type,
                actor,
                {
                    "question_id": str(question["question_id"]),
                    "name": str(question["name"]),
                    "reason": normalized_reason,
                },
            )
            connection.execute(
                """INSERT INTO question_visibility(name,visible,reason,question_id,changed_seq)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET visible=excluded.visible,
                     reason=excluded.reason,question_id=excluded.question_id,
                     changed_seq=excluded.changed_seq""",
                (
                    str(question["name"]),
                    int(visible),
                    normalized_reason,
                    str(question["question_id"]),
                    seq,
                ),
            )
        return str(question["question_id"])

    @staticmethod
    def _check_type_declaration(value_type: str) -> None:
        if value_type in {
            "Int",
            "Float",
            "Probability",
            "Bool",
            "String",
            "Json",
            "Date",
            "DateTime",
            "Year",
        }:
            return
        if value_type == "Interval[Date]":
            return
        if value_type.startswith("Enum[") and value_type.endswith("]"):
            if all(part.strip() for part in value_type[5:-1].split(",")):
                return
        if value_type.startswith("Ref[") and value_type.endswith("]"):
            if value_type[4:-1].strip():
                return
        if value_type.startswith("Quantity[") and value_type.endswith("]"):
            if value_type[9:-1].strip():
                return
        if value_type == "Distribution[Float]":
            return
        if value_type.startswith("Distribution[Enum[") and value_type.endswith("]]"):
            inner = value_type[13:-1]
            if all(part.strip() for part in inner[5:-1].split(",")):
                return
        raise EpiqError("value_type_error", f"Unknown or malformed value type: {value_type}")

    def add_evidence(
        self,
        url: str,
        title: str,
        retrieved_at: str,
        excerpt: str,
        actor: str,
        published_at: str | None = None,
        source_type: str = "web",
        locator: dict[str, Any] | None = None,
        source_entity: str | None = None,
    ) -> tuple[str, str]:
        """Atomically add a source and an immutable evidence fragment."""
        with self.transaction() as connection:
            return self._add_evidence_tx(
                connection,
                url,
                title,
                retrieved_at,
                excerpt,
                actor,
                published_at,
                source_type,
                None,
                locator,
                source_entity,
            )

    def _add_evidence_tx(
        self,
        connection: sqlite3.Connection,
        url: str,
        title: str,
        retrieved_at: str,
        excerpt: str,
        actor: str,
        published_at: str | None = None,
        source_type: str = "web",
        submitted_by: str | None = None,
        locator: dict[str, Any] | None = None,
        source_entity: str | None = None,
    ) -> tuple[str, str]:
        if source_type not in {"web", "personal", "model", "report", "interview", "other"}:
            raise EpiqError("invalid_source_type", f"Unknown evidence source type: {source_type}")
        canonical_url = canonicalize_url(url)
        normalized_excerpt = _normalize_excerpt(excerpt)
        normalized_locator = _json(locator or {})
        linked_entity_id = None
        if source_entity:
            linked_entity_id = str(self._resolve_entity(connection, source_entity)["entity_id"])
        if not normalized_excerpt:
            raise EpiqError("invalid_evidence", "Evidence excerpt cannot be empty")
        hash_material = (
            f"{canonical_url}\n{normalized_excerpt}\n{normalized_locator}\n{linked_entity_id or ''}"
        )
        source_hash = hashlib.sha256(hash_material.encode()).hexdigest()
        evidence_hash = hashlib.sha256(hash_material.encode()).hexdigest()
        existing = connection.execute(
            "SELECT evidence_id, source_id FROM evidence WHERE content_hash=?", (evidence_hash,)
        ).fetchone()
        if existing:
            return str(existing["source_id"]), str(existing["evidence_id"])
        source_id, evidence_id = _id("src"), _id("evd")
        seq, _, _ = self._event(
            connection,
            "evidence.add",
            actor,
            {
                "source_id": source_id,
                "evidence_id": evidence_id,
                "url": canonical_url,
                "source_type": source_type,
                "title": title,
                "retrieved_at": retrieved_at,
                "published_at": published_at,
                "excerpt": normalized_excerpt,
                "content_hash": evidence_hash,
                "locator": locator or {},
                "source_entity_id": linked_entity_id,
                **(
                    {"submitted_by": submitted_by} if submitted_by and submitted_by != actor else {}
                ),
            },
        )
        connection.execute(
            """INSERT INTO sources
               (source_id,url,source_type,title,retrieved_at,published_at,content_hash,
                locator_json,linked_entity_id,created_seq)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                source_id,
                canonical_url,
                source_type,
                title,
                retrieved_at,
                published_at,
                source_hash,
                normalized_locator,
                linked_entity_id,
                seq,
            ),
        )
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?,?)",
            (evidence_id, source_id, normalized_excerpt, evidence_hash, seq),
        )
        return source_id, evidence_id

    def assess_evidence(self, evidence_id: str, status: str, reason: str, actor: str) -> str:
        """Append a quality/status assessment without altering immutable captured evidence."""
        if status not in {"accepted", "disputed", "invalid", "superseded"}:
            raise EpiqError("invalid_evidence_status", f"Unknown evidence status: {status}")
        if not reason.strip():
            raise EpiqError("reason_required", "An evidence assessment reason is required")
        with self.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)
                ).fetchone()
                is None
            ):
                raise EpiqError("evidence_not_found", f"Evidence not found: {evidence_id}")
            assessment_id = _id("eas")
            seq, _, _ = self._event(
                connection,
                "evidence.assess",
                actor,
                {
                    "assessment_id": assessment_id,
                    "evidence_id": evidence_id,
                    "status": status,
                    "reason": reason.strip(),
                },
            )
            connection.execute(
                "INSERT INTO evidence_assessments VALUES(?,?,?,?,?)",
                (assessment_id, evidence_id, status, reason.strip(), seq),
            )
        return assessment_id

    def evidence_assessments(self, evidence_id: str) -> list[dict[str, Any]]:
        """Return the complete assessment history for an evidence fragment."""
        with self.connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)
                ).fetchone()
                is None
            ):
                raise EpiqError("evidence_not_found", f"Evidence not found: {evidence_id}")
            rows = connection.execute(
                """SELECT a.*,ev.recorded_at,ev.actor FROM evidence_assessments a
                   JOIN events ev ON ev.seq=a.created_seq
                   WHERE a.evidence_id=? ORDER BY a.created_seq""",
                (evidence_id,),
            ).fetchall()
        return [
            {
                "assessment_id": str(row["assessment_id"]),
                "evidence_id": str(row["evidence_id"]),
                "status": str(row["status"]),
                "reason": str(row["reason"]),
                "recorded_at": str(row["recorded_at"]),
                "actor": str(row["actor"]),
            }
            for row in rows
        ]

    def _follow_entity_redirect(
        self, connection: sqlite3.Connection, entity: sqlite3.Row
    ) -> sqlite3.Row:
        seen: set[str] = set()
        current = entity
        while True:
            entity_id = str(current["entity_id"])
            if entity_id in seen:
                raise EpiqError("entity_merge_cycle", f"Entity merge cycle includes {entity_id}")
            seen.add(entity_id)
            redirect = connection.execute(
                "SELECT into_entity_id FROM entity_redirects WHERE from_entity_id=?",
                (entity_id,),
            ).fetchone()
            if redirect is None:
                return current
            current = connection.execute(
                "SELECT * FROM entities WHERE entity_id=?", (redirect["into_entity_id"],)
            ).fetchone()

    def _resolve_entity(
        self, connection: sqlite3.Connection, reference: str, kind: str | None = None
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM entities WHERE entity_id=?" + (" AND kind=?" if kind else ""),
            (reference, kind) if kind else (reference,),
        ).fetchone()
        if row is None:
            row = self._find_entity_by_identity(connection, reference, kind)
        if not row:
            raise EpiqError("entity_not_found", f"Entity not found: {reference}")
        return self._follow_entity_redirect(connection, row)

    def _entity_cluster(self, connection: sqlite3.Connection, survivor_id: str) -> list[str]:
        rows = connection.execute(
            """WITH RECURSIVE cluster(entity_id) AS (
                   SELECT ?
                   UNION ALL
                   SELECT r.from_entity_id FROM entity_redirects r
                   JOIN cluster c ON r.into_entity_id=c.entity_id
               ) SELECT entity_id FROM cluster""",
            (survivor_id,),
        ).fetchall()
        return [str(row["entity_id"]) for row in rows]

    def _resolve_question(self, connection: sqlite3.Connection, reference: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM questions WHERE question_id=? OR name=? ORDER BY version DESC LIMIT 1",
            (reference, reference),
        ).fetchone()
        if not row:
            raise EpiqError("question_not_found", f"Question not found: {reference}")
        return row

    def assert_claim(
        self,
        subject: str,
        question: str,
        value: Any,
        valid_from: str,
        evidence_id: str | list[str],
        actor: str,
        recorded_at: str | None = None,
        confidence: str = "high",
        temporal_basis: str = "observed",
    ) -> str:
        """Assert a typed, evidence-backed claim."""
        with self.transaction() as connection:
            claim_id, _, _ = self._assert_claim_tx(
                connection,
                subject,
                question,
                value,
                valid_from,
                evidence_id,
                actor,
                recorded_at,
                confidence,
                temporal_basis,
            )
            return claim_id

    def assert_claims_bulk(self, claims: list[dict[str, Any]], actor: str) -> list[str]:
        """Validate and commit a claim batch atomically, preserving input order."""
        if not claims:
            raise EpiqError("empty_batch", "A claim batch must contain at least one item")
        if len(claims) > 1000:
            raise EpiqError("batch_too_large", "A claim batch cannot exceed 1,000 items")
        claim_ids: list[str] = []
        with self.transaction() as connection:
            for index, item in enumerate(claims):
                try:
                    claim_id, _, _ = self._assert_claim_tx(
                        connection,
                        str(item["subject"]),
                        str(item["question"]),
                        item["value"],
                        str(item["valid_from"]),
                        list(item["evidence_ids"]),
                        actor,
                        confidence=str(item.get("confidence", "high")),
                        temporal_basis=str(item.get("temporal_basis", "observed")),
                    )
                except KeyError as error:
                    raise EpiqError(
                        "invalid_batch_item",
                        f"Claim batch item {index} is missing {error.args[0]}",
                    ) from error
                except EpiqError as error:
                    raise EpiqError(
                        error.code,
                        f"Claim batch item {index}: {error.message}",
                        error.suggestion,
                    ) from error
                claim_ids.append(claim_id)
        return claim_ids

    def write_batch(self, operations: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
        """Atomically add evidence and dependent claims using batch-local evidence references."""
        if not operations:
            raise EpiqError("empty_batch", "A write batch must contain at least one operation")
        if len(operations) > 2000:
            raise EpiqError("batch_too_large", "A write batch cannot exceed 2,000 operations")
        with self.transaction() as connection:
            return self._write_batch_tx(connection, operations, actor)

    def _write_batch_tx(
        self, connection: sqlite3.Connection, operations: list[dict[str, Any]], actor: str
    ) -> list[dict[str, Any]]:
        evidence_refs: dict[str, str] = {}
        results: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            try:
                kind = str(operation["op"])
                operation_actor = str(operation.get("actor", actor))
                if kind == "evidence.add":
                    reference = str(operation.get("ref", "")).strip()
                    if reference and reference in evidence_refs:
                        raise EpiqError(
                            "duplicate_batch_ref", f"Duplicate evidence ref: {reference}"
                        )
                    source_id, evidence_id = self._add_evidence_tx(
                        connection,
                        str(operation["url"]),
                        str(operation["title"]),
                        str(operation["retrieved_at"]),
                        str(operation["excerpt"]),
                        operation_actor,
                        (
                            str(operation["published_at"])
                            if operation.get("published_at") is not None
                            else None
                        ),
                        str(operation.get("source_type", "web")),
                        actor,
                        dict(operation.get("locator", {})),
                        (
                            str(operation["source_entity"])
                            if operation.get("source_entity") is not None
                            else None
                        ),
                    )
                    if reference:
                        evidence_refs[reference] = evidence_id
                    results.append(
                        {
                            "op": kind,
                            "ref": reference or None,
                            "source_id": source_id,
                            "evidence_id": evidence_id,
                        }
                    )
                elif kind == "claim.assert":
                    evidence_ids = [str(item) for item in operation.get("evidence_ids", [])]
                    for reference in operation.get("evidence_refs", []):
                        key = str(reference)
                        if key not in evidence_refs:
                            raise EpiqError("batch_ref_not_found", f"Evidence ref not found: {key}")
                        evidence_ids.append(evidence_refs[key])
                    claim_id, _, _ = self._assert_claim_tx(
                        connection,
                        str(operation["subject"]),
                        str(operation["question"]),
                        operation["value"],
                        str(operation["valid_from"]),
                        evidence_ids,
                        operation_actor,
                        confidence=str(operation.get("confidence", "high")),
                        temporal_basis=str(operation.get("temporal_basis", "observed")),
                        extra_payload=(
                            {"submitted_by": actor} if operation_actor != actor else None
                        ),
                    )
                    results.append({"op": kind, "claim_id": claim_id})
                else:
                    raise EpiqError("unsupported_batch_operation", f"Unknown operation: {kind}")
            except KeyError as error:
                raise EpiqError(
                    "invalid_batch_item",
                    f"Write batch item {index} is missing {error.args[0]}",
                ) from error
            except EpiqError as error:
                raise EpiqError(
                    error.code,
                    f"Write batch item {index}: {error.message}",
                    error.suggestion,
                ) from error
        return results

    def apply_document(self, document: dict[str, Any], actor: str) -> dict[str, Any]:
        """Converge declarative schema, entities, evidence, and claims atomically."""
        allowed = {"project", "entity_kinds", "entities", "questions", "aliases", "operations"}
        unknown = set(document) - allowed
        if unknown:
            raise EpiqError("invalid_apply_document", f"Unknown apply keys: {sorted(unknown)}")
        requirements = {
            "entities": {"kind", "name"},
            "questions": {"name", "subject_kind", "value_type"},
            "aliases": {"entity", "alias"},
        }
        for section in ("entity_kinds", "entities", "questions", "aliases", "operations"):
            if not isinstance(document.get(section, []), list):
                raise EpiqError("invalid_apply_document", f"{section} must be a JSON array")
        for section, required in requirements.items():
            for index, item in enumerate(document.get(section, [])):
                if not isinstance(item, dict) or not required.issubset(item):
                    missing = sorted(required - set(item) if isinstance(item, dict) else required)
                    raise EpiqError(
                        "invalid_apply_document",
                        f"{section}[{index}] is missing required fields: {missing}",
                    )
        results: dict[str, list[dict[str, Any]]] = {
            "entity_kinds": [],
            "entities": [],
            "questions": [],
            "aliases": [],
            "operations": [],
        }
        with self.transaction() as connection:
            for raw_kind in document.get("entity_kinds", []):
                kind = str(raw_kind).strip()
                existing = connection.execute(
                    "SELECT 1 FROM entity_kinds WHERE kind=?", (kind,)
                ).fetchone()
                if existing:
                    results["entity_kinds"].append({"kind": kind, "status": "unchanged"})
                    continue
                seq, _, _ = self._event(connection, "entity_kind.define", actor, {"kind": kind})
                connection.execute("INSERT INTO entity_kinds VALUES(?,?)", (kind, seq))
                results["entity_kinds"].append({"kind": kind, "status": "created"})
            for item in document.get("entities", []):
                kind = str(item["kind"])
                identity = dict(item["identity"]) if item.get("identity") is not None else None
                name = str(item.get("name") or "")
                if not name and identity:
                    parts = ", ".join(f"{key}={value}" for key, value in sorted(identity.items()))
                    name = f"{kind}[{parts}]"
                if not name:
                    raise EpiqError("entity_name_required", "Entity requires name or identity")
                existing = self._find_entity_by_identity(connection, name, kind)
                if existing:
                    expected_attributes = dict(item.get("attributes", {}))
                    if json.loads(str(existing["attributes_json"])) != expected_attributes:
                        raise EpiqError(
                            "entity_definition_conflict",
                            f"Existing entity attributes differ: {kind} {name}",
                        )
                    entity_id, status = str(existing["entity_id"]), "unchanged"
                else:
                    entity_id = self._add_entity_tx(
                        connection,
                        kind,
                        name,
                        dict(item.get("attributes", {})),
                        actor,
                        str(item.get("role", "entity")),
                        identity,
                    )
                    status = "created"
                results["entities"].append(
                    {"entity_id": entity_id, "kind": kind, "name": name, "status": status}
                )
            for item in document.get("questions", []):
                name = str(item["name"])
                latest = connection.execute(
                    "SELECT * FROM questions WHERE name=? ORDER BY version DESC LIMIT 1", (name,)
                ).fetchone()
                definition = dict(item.get("definition", {}))
                desired = (
                    str(item["subject_kind"]),
                    str(item["value_type"]),
                    _json(definition),
                )
                current = (
                    (
                        str(latest["subject_kind"]),
                        str(latest["value_type"]),
                        str(latest["definition_json"]),
                    )
                    if latest
                    else None
                )
                if current == desired:
                    question_id, status = str(latest["question_id"]), "unchanged"
                else:
                    question_id, _ = self._add_question_tx(
                        connection, name, desired[0], desired[1], definition, actor
                    )
                    status = "created" if latest is None else "versioned"
                results["questions"].append(
                    {"question_id": question_id, "name": name, "status": status}
                )
            for item in document.get("aliases", []):
                target = self._resolve_entity(connection, str(item["entity"]))
                alias = str(item["alias"])
                existing = self._find_entity_by_identity(connection, alias)
                if existing:
                    existing = self._follow_entity_redirect(connection, existing)
                    if existing["entity_id"] != target["entity_id"]:
                        raise EpiqError(
                            "alias_conflict", f"Alias identifies another entity: {alias}"
                        )
                    results["aliases"].append({"alias": alias, "status": "unchanged"})
                    continue
                alias_id = _id("als")
                seq, _, _ = self._event(
                    connection,
                    "entity.alias",
                    actor,
                    {"alias_id": alias_id, "entity_id": target["entity_id"], "alias": alias},
                )
                connection.execute(
                    "INSERT INTO entity_aliases VALUES(?,?,?,?,?)",
                    (alias_id, target["entity_id"], alias, _identity_key(alias), seq),
                )
                results["aliases"].append(
                    {"alias_id": alias_id, "alias": alias, "status": "created"}
                )
            operations = list(document.get("operations", []))
            if operations:
                results["operations"] = self._write_batch_tx(connection, operations, actor)
        return {"ok": True, **results}

    def propose_claim(
        self,
        subject: str,
        question: str,
        value: Any,
        valid_from: str,
        evidence_ids: list[str],
        actor: str,
        confidence: str = "medium",
        temporal_basis: str = "observed",
        rationale: str = "",
    ) -> str:
        """Stage a fully validated claim outside current projections for later review."""
        with self.transaction() as connection:
            entity, q, normalized_value, evidence = self._validate_claim_candidate(
                connection,
                subject,
                question,
                value,
                evidence_ids,
                confidence,
                temporal_basis,
            )
            proposal_id = _id("prp")
            seq, _, _ = self._event(
                connection,
                "claim.propose",
                actor,
                {
                    "proposal_id": proposal_id,
                    "subject_id": str(entity["entity_id"]),
                    "question_id": str(q["question_id"]),
                    "value": normalized_value,
                    "valid_from": valid_from,
                    "evidence_ids": evidence,
                    "confidence": confidence,
                    "temporal_basis": temporal_basis,
                    "rationale": rationale.strip(),
                },
            )
            connection.execute(
                """INSERT INTO claim_proposals
                   VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,NULL)""",
                (
                    proposal_id,
                    entity["entity_id"],
                    q["question_id"],
                    _json(normalized_value),
                    valid_from,
                    _json(evidence),
                    confidence,
                    temporal_basis,
                    rationale.strip(),
                    seq,
                ),
            )
        return proposal_id

    def claim_proposals(self, status: str | None = "pending") -> list[dict[str, Any]]:
        """Return the durable claim review queue."""
        if status not in {None, "pending", "approved", "rejected"}:
            raise EpiqError("invalid_proposal_status", f"Unknown proposal status: {status}")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.*,e.name entity_name,q.name question_name
                   FROM claim_proposals p
                   JOIN entities e ON e.entity_id=p.subject_id
                   JOIN questions q ON q.question_id=p.question_id
                   WHERE (? IS NULL OR p.status=?) ORDER BY p.created_seq""",
                (status, status),
            ).fetchall()
        return [
            {
                "proposal_id": str(row["proposal_id"]),
                "subject_id": str(row["subject_id"]),
                "entity_name": str(row["entity_name"]),
                "question_id": str(row["question_id"]),
                "question_name": str(row["question_name"]),
                "value": json.loads(str(row["value_json"])),
                "valid_from": str(row["valid_from"]),
                "evidence_ids": json.loads(str(row["evidence_ids_json"])),
                "confidence": str(row["confidence"]),
                "temporal_basis": str(row["temporal_basis"]),
                "rationale": str(row["rationale"]),
                "status": str(row["status"]),
                "resulting_claim_id": row["resulting_claim_id"],
                "review_reason": row["review_reason"],
            }
            for row in rows
        ]

    def review_claim_proposals(
        self, proposal_ids: list[str], decision: str, reason: str, actor: str
    ) -> list[dict[str, Any]]:
        """Approve or reject a review selection in one all-or-nothing transaction."""
        identifiers = list(dict.fromkeys(proposal_ids))
        if not identifiers:
            raise EpiqError("empty_review", "Select at least one claim proposal")
        if decision not in {"approved", "rejected"}:
            raise EpiqError("invalid_review_decision", f"Unknown decision: {decision}")
        if not reason.strip():
            raise EpiqError("reason_required", "A review reason is required")
        results: list[dict[str, Any]] = []
        with self.transaction() as connection:
            placeholders = ",".join("?" for _ in identifiers)
            rows = connection.execute(
                f"SELECT * FROM claim_proposals WHERE proposal_id IN ({placeholders})",
                identifiers,
            ).fetchall()
            by_id = {str(row["proposal_id"]): row for row in rows}
            missing = [identifier for identifier in identifiers if identifier not in by_id]
            if missing:
                raise EpiqError("proposal_not_found", f"Claim proposal not found: {missing[0]}")
            inactive = [
                identifier for identifier in identifiers if by_id[identifier]["status"] != "pending"
            ]
            if inactive:
                raise EpiqError(
                    "proposal_already_reviewed", f"Proposal already reviewed: {inactive[0]}"
                )
            for proposal_id in identifiers:
                proposal = by_id[proposal_id]
                claim_id = None
                if decision == "approved":
                    claim_id, _, _ = self._assert_claim_tx(
                        connection,
                        str(proposal["subject_id"]),
                        str(proposal["question_id"]),
                        json.loads(str(proposal["value_json"])),
                        str(proposal["valid_from"]),
                        json.loads(str(proposal["evidence_ids_json"])),
                        actor,
                        confidence=str(proposal["confidence"]),
                        temporal_basis=str(proposal["temporal_basis"]),
                    )
                seq, _, _ = self._event(
                    connection,
                    f"claim.proposal_{decision}",
                    actor,
                    {
                        "proposal_id": proposal_id,
                        "claim_id": claim_id,
                        "reason": reason.strip(),
                    },
                )
                connection.execute(
                    """UPDATE claim_proposals SET status=?,resulting_claim_id=?,
                       review_reason=?,reviewed_seq=? WHERE proposal_id=?""",
                    (decision, claim_id, reason.strip(), seq, proposal_id),
                )
                results.append(
                    {"proposal_id": proposal_id, "status": decision, "claim_id": claim_id}
                )
        return results

    def _validate_claim_candidate(
        self,
        connection: sqlite3.Connection,
        subject: str,
        question: str,
        value: Any,
        evidence_ids: list[str],
        confidence: str,
        temporal_basis: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row, Any, list[str]]:
        q = self._resolve_question(connection, question)
        entity = self._resolve_entity(connection, subject)
        entity_visibility = connection.execute(
            "SELECT visible FROM entity_visibility WHERE entity_id=?", (entity["entity_id"],)
        ).fetchone()
        if entity_visibility is not None and not bool(entity_visibility["visible"]):
            raise EpiqError("entity_retired", f"Entity is retired: {entity['name']}")
        question_visibility = connection.execute(
            "SELECT visible FROM question_visibility WHERE name=?", (q["name"],)
        ).fetchone()
        if question_visibility is not None and not bool(question_visibility["visible"]):
            raise EpiqError("question_retired", f"Field is retired: {q['name']}")
        if entity["kind"] != q["subject_kind"]:
            raise EpiqError(
                "subject_type_mismatch",
                f"Question {q['name']} applies to {q['subject_kind']}, not {entity['kind']}",
            )
        evidence = list(dict.fromkeys(evidence_ids))
        if not evidence:
            raise EpiqError("evidence_required", "At least one evidence ID is required")
        placeholders = ",".join("?" for _ in evidence)
        found = {
            str(row["evidence_id"])
            for row in connection.execute(
                f"SELECT evidence_id FROM evidence WHERE evidence_id IN ({placeholders})", evidence
            )
        }
        missing = [item for item in evidence if item not in found]
        if missing:
            raise EpiqError("evidence_not_found", f"Evidence not found: {', '.join(missing)}")
        if confidence not in {"low", "medium", "high"}:
            raise EpiqError("confidence_error", f"Unknown confidence: {confidence}")
        if temporal_basis not in {"observed", "source", "unknown"}:
            raise EpiqError("temporal_basis_error", f"Unknown temporal basis: {temporal_basis}")
        value_type = str(q["value_type"])
        if value_type.startswith("Ref[") and value_type.endswith("]"):
            if not isinstance(value, str):
                raise EpiqError(
                    "value_type_error", f"Expected entity reference; received {value!r}"
                )
            target = self._resolve_entity(connection, value)
            expected = value_type[4:-1].strip()
            if target["kind"] != expected:
                raise EpiqError(
                    "reference_type_mismatch",
                    f"Expected reference to {expected}, not {target['kind']}",
                )
            value = str(target["entity_id"])
        self._check_value_type(value_type, value)
        return entity, q, value, evidence

    def _assert_claim_tx(
        self,
        connection: sqlite3.Connection,
        subject: str,
        question: str,
        value: Any,
        valid_from: str,
        evidence: str | list[str],
        actor: str,
        recorded_at: str | None = None,
        confidence: str = "high",
        temporal_basis: str = "observed",
        event_type: str = "claim.assert",
        extra_payload: dict[str, Any] | None = None,
    ) -> tuple[str, int, bool]:
        q = self._resolve_question(connection, question)
        entity = self._resolve_entity(connection, subject)
        entity_visibility = connection.execute(
            "SELECT visible FROM entity_visibility WHERE entity_id=?", (entity["entity_id"],)
        ).fetchone()
        if entity_visibility is not None and not bool(entity_visibility["visible"]):
            raise EpiqError(
                "entity_retired",
                f"Entity is retired: {entity['name']}",
                "Restore the entity before asserting new claims.",
            )
        visibility = connection.execute(
            "SELECT visible FROM question_visibility WHERE name=?", (q["name"],)
        ).fetchone()
        if visibility is not None and not bool(visibility["visible"]):
            raise EpiqError(
                "question_retired",
                f"Field is retired: {q['name']}",
                "Restore the field before asserting new claims.",
            )
        if entity["kind"] != q["subject_kind"]:
            raise EpiqError(
                "subject_type_mismatch",
                f"Question {q['name']} applies to {q['subject_kind']}, not {entity['kind']}",
            )
        evidence_ids = list(dict.fromkeys([evidence] if isinstance(evidence, str) else evidence))
        if not evidence_ids:
            raise EpiqError("evidence_required", "At least one evidence ID is required")
        placeholders = ",".join("?" for _ in evidence_ids)
        found = {
            str(row["evidence_id"])
            for row in connection.execute(
                f"SELECT evidence_id FROM evidence WHERE evidence_id IN ({placeholders})",
                evidence_ids,
            )
        }
        missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in found]
        if missing:
            raise EpiqError("evidence_not_found", f"Evidence not found: {', '.join(missing)}")
        if confidence not in {"low", "medium", "high"}:
            raise EpiqError("confidence_error", f"Unknown confidence: {confidence}")
        if temporal_basis not in {"observed", "source", "unknown"}:
            raise EpiqError("temporal_basis_error", f"Unknown temporal basis: {temporal_basis}")
        value_type = str(q["value_type"])
        if value_type.startswith("Ref[") and value_type.endswith("]"):
            if not isinstance(value, str):
                raise EpiqError(
                    "value_type_error", f"Expected entity reference; received {value!r}"
                )
            target_kind = value_type[4:-1].strip()
            target = self._resolve_entity(connection, value)
            if target["kind"] != target_kind:
                raise EpiqError(
                    "reference_type_mismatch",
                    f"Expected reference to {target_kind}, not {target['kind']}",
                )
            value = str(target["entity_id"])
        self._check_value_type(value_type, value)
        value_json = _json(value)
        primary_evidence = evidence_ids[0]
        existing = connection.execute(
            """SELECT claim_id,created_seq FROM claims WHERE subject_id=? AND question_id=?
               AND value_json=? AND valid_from=? AND evidence_id=?""",
            (entity["entity_id"], q["question_id"], value_json, valid_from, primary_evidence),
        ).fetchone()
        if existing:
            claim_id = str(existing["claim_id"])
            linked = {
                str(row["evidence_id"])
                for row in connection.execute(
                    "SELECT evidence_id FROM claim_evidence WHERE claim_id=?", (claim_id,)
                )
            }
            additions = [item for item in evidence_ids if item not in linked]
            if additions:
                seq, _, _ = self._event(
                    connection,
                    "claim.evidence_link",
                    actor,
                    {"claim_id": claim_id, "evidence_ids": additions},
                )
                next_ordinal = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(ordinal),-1)+1 n FROM claim_evidence WHERE claim_id=?",
                        (claim_id,),
                    ).fetchone()["n"]
                )
                connection.executemany(
                    "INSERT INTO claim_evidence VALUES(?,?,?,?)",
                    [
                        (claim_id, evidence_id, next_ordinal + offset, seq)
                        for offset, evidence_id in enumerate(additions)
                    ],
                )
                return claim_id, seq, False
            return claim_id, int(existing["created_seq"]), False
        claim_id = _id("clm")
        payload = {
            "claim_id": claim_id,
            "subject_id": entity["entity_id"],
            "question_id": q["question_id"],
            "value": value,
            "valid_from": valid_from,
            "evidence_id": primary_evidence,
            "evidence_ids": evidence_ids,
            "confidence": confidence,
            "temporal_basis": temporal_basis,
            **(extra_payload or {}),
        }
        seq, _, event_time = self._event(connection, event_type, actor, payload)
        tx_from = recorded_at or event_time
        connection.execute(
            """INSERT INTO claims
               (claim_id,subject_id,question_id,value_json,valid_from,valid_to,tx_from,tx_to,
                evidence_id,confidence,temporal_basis,status,created_seq,closed_seq)
               VALUES(?,?,?,?,?,NULL,?,NULL,?,?,?,'asserted',?,NULL)""",
            (
                claim_id,
                entity["entity_id"],
                q["question_id"],
                value_json,
                valid_from,
                tx_from,
                primary_evidence,
                confidence,
                temporal_basis,
                seq,
            ),
        )
        connection.executemany(
            "INSERT INTO claim_evidence VALUES(?,?,?,?)",
            [
                (claim_id, evidence_id, ordinal, seq)
                for ordinal, evidence_id in enumerate(evidence_ids)
            ],
        )
        return claim_id, seq, True

    def derive_distribution(
        self,
        subject: str,
        question: str,
        input_claim_ids: list[str],
        valid_from: str,
        actor: str,
        weights: list[float] | None = None,
        confidence: str = "medium",
    ) -> str:
        """Derive an empirical distribution with claim and evidence lineage."""
        inputs = list(dict.fromkeys(input_claim_ids))
        if not inputs:
            raise EpiqError("input_claims_required", "At least one input claim is required")
        if weights is not None and len(weights) != len(inputs):
            raise EpiqError("distribution_error", "Weights must match the number of input claims")
        with self.transaction() as connection:
            samples: list[int | float] = []
            evidence_ids: list[str] = []
            for claim_id in inputs:
                claim = connection.execute(
                    "SELECT * FROM claims WHERE claim_id=? AND status='asserted' AND tx_to IS NULL",
                    (claim_id,),
                ).fetchone()
                if not claim:
                    raise EpiqError("claim_not_found", f"Active input claim not found: {claim_id}")
                sample = json.loads(claim["value_json"])
                if not isinstance(sample, int | float) or isinstance(sample, bool):
                    raise EpiqError("distribution_error", f"Input claim is not numeric: {claim_id}")
                samples.append(sample)
                evidence_ids.extend(
                    str(row["evidence_id"])
                    for row in connection.execute(
                        """SELECT evidence_id FROM claim_evidence
                           WHERE claim_id=? ORDER BY ordinal""",
                        (claim_id,),
                    )
                )
            distribution: dict[str, Any] = {"kind": "empirical", "samples": samples}
            if weights is not None:
                distribution = {
                    "kind": "weighted_empirical",
                    "samples": samples,
                    "weights": weights,
                }
            operation = str(distribution["kind"])
            claim_id, seq, _ = self._assert_claim_tx(
                connection,
                subject,
                question,
                distribution,
                valid_from,
                list(dict.fromkeys(evidence_ids)),
                actor,
                confidence=confidence,
                event_type="claim.derive",
                extra_payload={"operation": operation, "input_claim_ids": inputs},
            )
            connection.execute(
                "INSERT OR IGNORE INTO derivations VALUES(?,?,?,?)",
                (claim_id, operation, _json({"weights": weights}), seq),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO claim_inputs VALUES(?,?,?,?)",
                [
                    (claim_id, input_claim_id, ordinal, seq)
                    for ordinal, input_claim_id in enumerate(inputs)
                ],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO derivation_dependencies VALUES(?,?,?,?,?)",
                [
                    (claim_id, input_claim_id, "operand", ordinal, seq)
                    for ordinal, input_claim_id in enumerate(inputs)
                ],
            )
            return claim_id

    def derive_claim(
        self,
        subject: str,
        question: str,
        operation: str,
        input_claim_ids: list[str],
        valid_from: str,
        actor: str,
        parameters: dict[str, Any] | None = None,
        confidence: str = "medium",
        parameter_claim_ids: list[str] | None = None,
        path_claim_ids: list[str] | None = None,
    ) -> str:
        """Compute and persist a typed claim with complete input-claim lineage."""
        allowed = {"sum", "avg", "min", "max", "count", "weighted_avg", "linear", "copy"}
        if operation not in allowed:
            raise EpiqError("invalid_derivation", f"Unknown derivation operation: {operation}")
        inputs = list(dict.fromkeys(input_claim_ids))
        if not inputs:
            raise EpiqError("input_claims_required", "At least one input claim is required")
        params = dict(parameters or {})
        parameter_inputs = list(dict.fromkeys(parameter_claim_ids or []))
        path_inputs = list(dict.fromkeys(path_claim_ids or []))
        with self.transaction() as connection:
            values: list[Any] = []
            evidence_ids: list[str] = []
            for claim_id in inputs:
                claim = connection.execute(
                    "SELECT * FROM claims WHERE claim_id=? AND status='asserted' AND tx_to IS NULL",
                    (claim_id,),
                ).fetchone()
                if not claim:
                    raise EpiqError("claim_not_found", f"Active input claim not found: {claim_id}")
                values.append(json.loads(str(claim["value_json"])))
                evidence_ids.extend(
                    str(row["evidence_id"])
                    for row in connection.execute(
                        "SELECT evidence_id FROM claim_evidence WHERE claim_id=? ORDER BY ordinal",
                        (claim_id,),
                    )
                )
            parameter_values: list[Any] = []
            for claim_id in parameter_inputs:
                claim = connection.execute(
                    "SELECT * FROM claims WHERE claim_id=? AND status='asserted' AND tx_to IS NULL",
                    (claim_id,),
                ).fetchone()
                if not claim:
                    raise EpiqError(
                        "claim_not_found", f"Active parameter claim not found: {claim_id}"
                    )
                parameter_values.append(json.loads(str(claim["value_json"])))
                evidence_ids.extend(
                    str(row["evidence_id"])
                    for row in connection.execute(
                        "SELECT evidence_id FROM claim_evidence WHERE claim_id=? ORDER BY ordinal",
                        (claim_id,),
                    )
                )
            for claim_id in path_inputs:
                claim = connection.execute(
                    "SELECT * FROM claims WHERE claim_id=? AND status='asserted' AND tx_to IS NULL",
                    (claim_id,),
                ).fetchone()
                if not claim:
                    raise EpiqError("claim_not_found", f"Active path claim not found: {claim_id}")
                evidence_ids.extend(
                    str(row["evidence_id"])
                    for row in connection.execute(
                        "SELECT evidence_id FROM claim_evidence WHERE claim_id=? ORDER BY ordinal",
                        (claim_id,),
                    )
                )
            if operation == "count":
                result: Any = len(values)
            elif operation == "copy":
                if len(values) != 1:
                    raise EpiqError("invalid_derivation_parameters", "copy requires one input")
                result = values[0]
            else:
                numeric = all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in values
                )
                if not numeric:
                    raise EpiqError(
                        "non_numeric_derivation", f"{operation} requires numeric claims"
                    )
                if operation == "sum":
                    result = sum(values)
                elif operation == "avg":
                    result = sum(values) / len(values)
                elif operation == "min":
                    result = min(values)
                elif operation == "max":
                    result = max(values)
                elif operation == "weighted_avg":
                    weights = parameter_values or params.get("weights")
                    if not isinstance(weights, list) or len(weights) != len(values):
                        raise EpiqError(
                            "invalid_derivation_parameters",
                            "weighted_avg requires one weight per input claim",
                        )
                    if not all(
                        isinstance(weight, (int, float))
                        and not isinstance(weight, bool)
                        and math.isfinite(weight)
                        and weight >= 0
                        for weight in weights
                    ):
                        raise EpiqError("invalid_derivation_parameters", "Weights must be finite")
                    total_weight = sum(weights)
                    if total_weight <= 0:
                        raise EpiqError("invalid_derivation_parameters", "Weights must sum above 0")
                    weighted_sum = sum(
                        value * weight for value, weight in zip(values, weights, strict=True)
                    )
                    result = weighted_sum / total_weight
                else:
                    if len(values) != 1:
                        raise EpiqError(
                            "invalid_derivation_parameters", "linear requires one input"
                        )
                    scale, offset = params.get("scale", 1), params.get("offset", 0)
                    if not all(isinstance(item, (int, float)) for item in (scale, offset)):
                        raise EpiqError(
                            "invalid_derivation_parameters", "scale and offset must be numeric"
                        )
                    result = values[0] * scale + offset
            claim_id, seq, _ = self._assert_claim_tx(
                connection,
                subject,
                question,
                result,
                valid_from,
                list(dict.fromkeys(evidence_ids)),
                actor,
                confidence=confidence,
                event_type="claim.derive",
                extra_payload={
                    "operation": operation,
                    "input_claim_ids": inputs,
                    "parameter_claim_ids": parameter_inputs,
                    "path_claim_ids": path_inputs,
                },
            )
            if parameter_inputs:
                params["parameter_claim_ids"] = parameter_inputs
            if path_inputs:
                params["path_claim_ids"] = path_inputs
            connection.execute(
                "INSERT OR IGNORE INTO derivations VALUES(?,?,?,?)",
                (claim_id, operation, _json(params), seq),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO claim_inputs VALUES(?,?,?,?)",
                [(claim_id, input_id, ordinal, seq) for ordinal, input_id in enumerate(inputs)],
            )
            dependencies = [
                (claim_id, input_id, "operand", ordinal, seq)
                for ordinal, input_id in enumerate(inputs)
            ]
            dependencies.extend(
                (claim_id, input_id, "parameter", ordinal, seq)
                for ordinal, input_id in enumerate(parameter_inputs)
            )
            dependencies.extend(
                (claim_id, input_id, "path", ordinal, seq)
                for ordinal, input_id in enumerate(path_inputs)
            )
            connection.executemany(
                "INSERT OR IGNORE INTO derivation_dependencies VALUES(?,?,?,?,?)", dependencies
            )
            return claim_id

    def materialize_formulas(
        self,
        kind: str,
        valid_from: str,
        actor: str,
        subjects: list[str] | None = None,
        questions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Evaluate declarative per-row formulas stored in question definitions."""
        projection = self.matrix(kind)
        selected = set(subjects or [])
        rows = [row for row in projection["rows"] if not selected or row["name"] in selected]
        formulas = []
        selected_questions = set(questions or [])
        for question in projection["questions"]:
            formula = question["definition"].get("formula")
            if formula is not None and (
                not selected_questions or question["name"] in selected_questions
            ):
                if not isinstance(formula, dict):
                    raise EpiqError(
                        "invalid_formula", f"Formula for {question['name']} must be an object"
                    )
                formulas.append((str(question["name"]), formula))
        results: list[dict[str, Any]] = []
        for row in rows:
            for target, formula in formulas:
                operation = str(formula.get("operation", ""))
                inputs = formula.get("inputs")
                if not isinstance(inputs, list) or not all(
                    isinstance(item, str) for item in inputs
                ):
                    raise EpiqError(
                        "invalid_formula", f"Formula for {target} requires string inputs"
                    )
                input_ids: list[str] = []
                missing = False
                for source_question in inputs:
                    try:
                        input_ids.extend(
                            self.active_claim_ids(str(row["entity_id"]), source_question)
                        )
                    except EpiqError as exc:
                        if exc.code != "claim_not_found":
                            raise
                        missing = True
                if missing:
                    results.append(
                        {"subject": row["name"], "question": target, "status": "skipped"}
                    )
                    continue
                claim_id = self.derive_claim(
                    str(row["entity_id"]),
                    target,
                    operation,
                    input_ids,
                    valid_from,
                    actor,
                    formula.get("parameters", {}),
                    str(formula.get("confidence", "medium")),
                )
                results.append(
                    {
                        "subject": row["name"],
                        "question": target,
                        "status": "materialized",
                        "claim_id": claim_id,
                    }
                )
        return {"ok": True, "kind": kind, "count": len(results), "results": results}

    def propagate_claim(
        self,
        subject: str,
        via: str | None,
        source_question: str,
        target_question: str,
        direction: str,
        depth: int,
        valid_from: str,
        actor: str,
        confidence: str = "medium",
    ) -> tuple[str, str]:
        """Materialize a claim found through a relationship path."""
        traversal = self.related(subject, via, direction, depth)
        candidates: list[tuple[int, str, str, list[str]]] = []
        for edge in traversal["edges"]:
            endpoint = edge["to"] if edge["direction"] == "outgoing" else edge["from"]
            try:
                claims = self.active_claim_ids(str(endpoint["entity_id"]), source_question)
            except EpiqError as exc:
                if exc.code == "claim_not_found":
                    continue
                raise
            candidates.extend(
                (
                    int(edge["depth"]),
                    str(endpoint["name"]),
                    item,
                    list(edge["path_claim_ids"]),
                )
                for item in claims
            )
        if not candidates:
            raise EpiqError("claim_not_found", "No related entity has an active source claim")
        nearest_depth = min(item[0] for item in candidates)
        nearest = [item for item in candidates if item[0] == nearest_depth]
        if len(nearest) != 1:
            raise EpiqError(
                "ambiguous_propagation", "Multiple equally near source claims were found"
            )
        _, source_name, input_id, path_claim_ids = nearest[0]
        claim_id = self.derive_claim(
            subject,
            target_question,
            "copy",
            [input_id],
            valid_from,
            actor,
            {
                "via": via,
                "direction": direction,
                "depth": nearest_depth,
                "source_entity": source_name,
            },
            confidence,
            path_claim_ids=path_claim_ids,
        )
        return claim_id, source_name

    def active_claim_ids(self, subject: str, question: str) -> list[str]:
        """Resolve current claim IDs for ergonomic derivation inputs."""
        with self.connect() as connection:
            entity = self._resolve_entity(connection, subject)
            field = self._resolve_question(connection, question)
            rows = connection.execute(
                """SELECT claim_id FROM claims
                   WHERE subject_id=? AND question_id=? AND status='asserted' AND tx_to IS NULL
                   ORDER BY created_seq""",
                (entity["entity_id"], field["question_id"]),
            ).fetchall()
        if not rows:
            raise EpiqError("claim_not_found", f"No active claim for {subject} / {question}")
        return [str(row["claim_id"]) for row in rows]

    def stale_derivations(self, kind: str | None = None) -> dict[str, Any]:
        """Report active derived claims whose typed dependencies are no longer current."""
        with self.connect() as connection:
            derived = connection.execute(
                """SELECT c.claim_id,c.subject_id,c.question_id,c.created_seq,
                          e.name,q.name question,e.kind
                   FROM claims c JOIN derivations d ON d.claim_id=c.claim_id
                   JOIN entities e ON e.entity_id=c.subject_id
                   JOIN questions q ON q.question_id=c.question_id
                   WHERE c.status='asserted' AND c.tx_to IS NULL
                   AND (? IS NULL OR e.kind=?) ORDER BY c.created_seq""",
                (kind, kind),
            ).fetchall()
            items: list[dict[str, Any]] = []
            stale_ids: set[str] = set()
            pending = list(derived)
            while pending:
                progressed = False
                deferred: list[sqlite3.Row] = []
                for claim in pending:
                    dependencies = connection.execute(
                        """SELECT dd.role,dd.dependency_claim_id,c.status,c.tx_to,c.subject_id,
                                  c.question_id,c.created_seq
                           FROM derivation_dependencies dd
                           JOIN claims c ON c.claim_id=dd.dependency_claim_id
                           WHERE dd.claim_id=? ORDER BY dd.role,dd.ordinal""",
                        (claim["claim_id"],),
                    ).fetchall()
                    unresolved_derived = [
                        row
                        for row in dependencies
                        if connection.execute(
                            "SELECT 1 FROM derivations WHERE claim_id=?",
                            (row["dependency_claim_id"],),
                        ).fetchone()
                        and str(row["dependency_claim_id"]) not in stale_ids
                        and any(
                            str(other["claim_id"]) == str(row["dependency_claim_id"])
                            for other in pending
                            if str(other["claim_id"]) != str(claim["claim_id"])
                        )
                    ]
                    if unresolved_derived:
                        deferred.append(claim)
                        continue
                    reasons: list[dict[str, Any]] = []
                    for dependency in dependencies:
                        dependency_id = str(dependency["dependency_claim_id"])
                        reason = None
                        if dependency_id in stale_ids:
                            reason = "dependency_stale"
                        elif dependency["status"] != "asserted" or dependency["tx_to"] is not None:
                            reason = "dependency_inactive"
                        else:
                            newer = connection.execute(
                                """SELECT claim_id FROM claims WHERE subject_id=? AND question_id=?
                                   AND status='asserted' AND tx_to IS NULL AND created_seq>?
                                   ORDER BY created_seq DESC LIMIT 1""",
                                (
                                    dependency["subject_id"],
                                    dependency["question_id"],
                                    dependency["created_seq"],
                                ),
                            ).fetchone()
                            if newer:
                                reason = "newer_claim_available"
                        if reason:
                            reasons.append(
                                {
                                    "dependency_claim_id": dependency_id,
                                    "role": str(dependency["role"]),
                                    "reason": reason,
                                }
                            )
                    if reasons:
                        stale_ids.add(str(claim["claim_id"]))
                        items.append(
                            {
                                "claim_id": str(claim["claim_id"]),
                                "subject": str(claim["name"]),
                                "kind": str(claim["kind"]),
                                "question": str(claim["question"]),
                                "reasons": reasons,
                            }
                        )
                    progressed = True
                if not deferred or not progressed:
                    break
                pending = deferred
        return {"count": len(items), "stale_derivations": items}

    @staticmethod
    def _check_value_type(value_type: str, value: Any) -> None:
        if value_type == "Probability":
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise EpiqError(
                    "value_type_error", f"Expected Probability between 0 and 1; received {value!r}"
                )
        elif value_type.startswith("Distribution[") and value_type.endswith("]"):
            Store._check_distribution(value_type[13:-1], value)
        elif value_type.startswith("Enum["):
            choices = [part.strip() for part in value_type[5:-1].split(",")]
            if value not in choices:
                raise EpiqError(
                    "value_type_error", f"Expected one of {choices}; received {value!r}"
                )
        elif value_type == "Int" and (not isinstance(value, int) or isinstance(value, bool)):
            raise EpiqError("value_type_error", f"Expected Int; received {value!r}")
        elif value_type == "Float" and (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise EpiqError("value_type_error", f"Expected finite Float; received {value!r}")
        elif value_type == "Bool" and not isinstance(value, bool):
            raise EpiqError("value_type_error", f"Expected Bool; received {value!r}")
        elif value_type == "String" and not isinstance(value, str):
            raise EpiqError("value_type_error", f"Expected String; received {value!r}")
        elif value_type == "Date":
            if not isinstance(value, str):
                raise EpiqError("value_type_error", f"Expected ISO Date; received {value!r}")
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError as error:
                raise EpiqError(
                    "value_type_error", f"Expected ISO Date YYYY-MM-DD; received {value!r}"
                ) from error
        elif value_type == "DateTime":
            if not isinstance(value, str):
                raise EpiqError("value_type_error", f"Expected ISO DateTime; received {value!r}")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            except ValueError as error:
                raise EpiqError(
                    "value_type_error", f"Expected timezone-aware ISO DateTime; received {value!r}"
                ) from error
        elif value_type == "Year" and (
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 9999
        ):
            raise EpiqError(
                "value_type_error", f"Expected Year integer 1..9999; received {value!r}"
            )
        elif value_type == "Interval[Date]":
            if not isinstance(value, dict) or set(value) - {"start", "end"} or "start" not in value:
                raise EpiqError(
                    "value_type_error", "Expected Interval[Date] object with start and optional end"
                )
            Store._check_value_type("Date", value["start"])
            if value.get("end") is not None:
                Store._check_value_type("Date", value["end"])
                if value["end"] <= value["start"]:
                    raise EpiqError("value_type_error", "Interval end must be after start")
        elif value_type.startswith("Ref[") and value_type.endswith("]"):
            if not isinstance(value, str):
                raise EpiqError(
                    "value_type_error", f"Expected entity reference; received {value!r}"
                )
        elif value_type.startswith("Quantity[") and value_type.endswith("]"):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise EpiqError("value_type_error", f"Expected finite quantity; received {value!r}")
        elif value_type not in {
            "Int",
            "Float",
            "Bool",
            "String",
            "Json",
            "Probability",
            "Date",
            "DateTime",
            "Year",
            "Interval[Date]",
        }:
            raise EpiqError("value_type_error", f"Unknown value type: {value_type}")

    @staticmethod
    def _check_distribution(inner_type: str, value: Any) -> None:
        if not isinstance(value, dict):
            raise EpiqError("value_type_error", "A distribution must be a JSON object")
        kind = value.get("kind")
        if inner_type == "Float" and kind in {"empirical", "weighted_empirical"}:
            samples = value.get("samples")
            if not isinstance(samples, list) or not samples:
                raise EpiqError("value_type_error", "An empirical distribution needs samples")
            if any(
                not isinstance(sample, int | float)
                or isinstance(sample, bool)
                or not math.isfinite(sample)
                for sample in samples
            ):
                raise EpiqError("value_type_error", "Distribution samples must be finite numbers")
            if kind == "weighted_empirical":
                weights = value.get("weights")
                if not isinstance(weights, list) or len(weights) != len(samples):
                    raise EpiqError(
                        "value_type_error", "Distribution weights must match the samples"
                    )
                if any(
                    not isinstance(weight, int | float)
                    or isinstance(weight, bool)
                    or not math.isfinite(weight)
                    or weight < 0
                    for weight in weights
                ) or not math.isclose(sum(weights), 1.0, rel_tol=0, abs_tol=1e-9):
                    raise EpiqError(
                        "value_type_error", "Distribution weights must be nonnegative and sum to 1"
                    )
            return
        if inner_type.startswith("Enum[") and inner_type.endswith("]") and kind == "categorical":
            choices = {part.strip() for part in inner_type[5:-1].split(",")}
            probabilities = value.get("probabilities")
            if not isinstance(probabilities, dict) or set(probabilities) != choices:
                raise EpiqError(
                    "value_type_error",
                    f"Categorical distribution must define exactly {sorted(choices)}",
                )
            values = list(probabilities.values())
            if any(
                not isinstance(probability, int | float)
                or isinstance(probability, bool)
                or not math.isfinite(probability)
                or probability < 0
                for probability in values
            ) or not math.isclose(sum(values), 1.0, rel_tol=0, abs_tol=1e-9):
                raise EpiqError(
                    "value_type_error", "Categorical probabilities must be nonnegative and sum to 1"
                )
            return
        raise EpiqError("value_type_error", f"Unsupported {kind!r} distribution for {inner_type}")

    def end_claim_validity(self, claim_id: str, valid_to: str, reason: str, actor: str) -> None:
        """Record when a fact stopped being true, independently of when Epiq learned it."""
        if not reason.strip():
            raise EpiqError("reason_required", "A validity-end reason is required")
        with self.transaction() as connection:
            claim = connection.execute(
                "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if claim is None:
                raise EpiqError("claim_not_found", f"Claim not found: {claim_id}")
            if valid_to <= str(claim["valid_from"]):
                raise EpiqError(
                    "invalid_validity_interval",
                    f"valid_to must be after valid_from ({claim['valid_from']})",
                )
            if connection.execute(
                "SELECT 1 FROM claim_validity_ends WHERE claim_id=?", (claim_id,)
            ).fetchone():
                raise EpiqError(
                    "validity_already_ended", f"Claim validity already ended: {claim_id}"
                )
            seq, _, _ = self._event(
                connection,
                "claim.validity_end",
                actor,
                {"claim_id": claim_id, "valid_to": valid_to, "reason": reason.strip()},
            )
            connection.execute(
                "INSERT INTO claim_validity_ends VALUES(?,?,?,?)",
                (claim_id, valid_to, reason.strip(), seq),
            )

    def close_claim(self, claim_id: str, status: str, reason: str, actor: str) -> None:
        """Retract or supersede a claim without deleting it."""
        if status not in {"retracted", "superseded"}:
            raise ValueError(status)
        with self.transaction() as connection:
            claim = connection.execute(
                "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if not claim:
                raise EpiqError("claim_not_found", f"Claim not found: {claim_id}")
            if claim["status"] != "asserted":
                raise EpiqError("claim_inactive", f"Claim is already {claim['status']}: {claim_id}")
            seq, _, event_time = self._event(
                connection, f"claim.{status}", actor, {"claim_id": claim_id, "reason": reason}
            )
            connection.execute(
                "UPDATE claims SET status=?, tx_to=?, closed_seq=? WHERE claim_id=?",
                (status, event_time, seq, claim_id),
            )

    def supersede_claim(
        self,
        claim_id: str,
        value: Any,
        valid_from: str,
        evidence: str | list[str],
        reason: str,
        actor: str,
        confidence: str = "high",
        temporal_basis: str = "observed",
    ) -> str:
        """Atomically close one claim and assert its evidence-backed replacement."""
        with self.transaction() as connection:
            old = connection.execute(
                "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if not old:
                raise EpiqError("claim_not_found", f"Claim not found: {claim_id}")
            if old["status"] != "asserted":
                raise EpiqError("claim_inactive", f"Claim is already {old['status']}: {claim_id}")
            replacement_id, seq, created = self._assert_claim_tx(
                connection,
                str(old["subject_id"]),
                str(old["question_id"]),
                value,
                valid_from,
                evidence,
                actor,
                confidence=confidence,
                temporal_basis=temporal_basis,
                event_type="claim.supersede",
                extra_payload={"supersedes_claim_id": claim_id, "reason": reason},
            )
            if not created or replacement_id == claim_id:
                raise EpiqError(
                    "replacement_duplicate",
                    "The replacement is already recorded and cannot atomically "
                    "supersede this claim",
                )
            event_time = str(
                connection.execute("SELECT recorded_at FROM events WHERE seq=?", (seq,)).fetchone()[
                    "recorded_at"
                ]
            )
            connection.execute(
                """UPDATE claims SET status='superseded',tx_to=?,closed_seq=?
                   WHERE claim_id=?""",
                (event_time, seq, claim_id),
            )
            return replacement_id

    def record_claim_feedback(self, claim_id: str, reason: str, guidance: str, actor: str) -> None:
        """Append human feedback about a claim without rewriting its history."""
        with self.transaction() as connection:
            claim = connection.execute(
                "SELECT claim_id FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if not claim:
                raise EpiqError("claim_not_found", f"Claim not found: {claim_id}")
            self._event(
                connection,
                "claim.feedback",
                actor,
                {"claim_id": claim_id, "reason": reason, "research_guidance": guidance},
            )

    def challenge_question(
        self,
        question: str,
        problem: str,
        explanation: str,
        actor: str,
        example_entity: str | None = None,
        evidence_ids: list[str] | None = None,
        proposed_replacement: dict[str, Any] | None = None,
    ) -> str:
        """Record that a question cannot faithfully represent an observation."""
        if problem not in QUESTION_CHALLENGE_PROBLEMS:
            raise EpiqError(
                "invalid_challenge_problem",
                f"Unknown question challenge problem: {problem}",
                f"Choose one of: {', '.join(sorted(QUESTION_CHALLENGE_PROBLEMS))}",
            )
        explanation = explanation.strip()
        if not explanation:
            raise EpiqError("invalid_challenge", "Challenge explanation cannot be empty")
        evidence_ids = list(dict.fromkeys(evidence_ids or []))
        if proposed_replacement is not None:
            if not isinstance(proposed_replacement, dict):
                raise EpiqError("invalid_replacement", "Proposed replacement must be an object")
            replacements = proposed_replacement.get("questions")
            if replacements is not None:
                if not isinstance(replacements, list) or not replacements:
                    raise EpiqError(
                        "invalid_replacement", "Replacement questions must be a non-empty list"
                    )
                for replacement in replacements:
                    if not isinstance(replacement, dict) or not {
                        "name",
                        "value_type",
                    }.issubset(replacement):
                        raise EpiqError(
                            "invalid_replacement",
                            "Every replacement question requires name and value_type",
                        )
                    self._check_type_declaration(str(replacement["value_type"]))
            elif not {"name", "value_type"}.issubset(proposed_replacement):
                raise EpiqError(
                    "invalid_replacement",
                    "Proposed replacement requires name and value_type, or questions",
                )
            else:
                self._check_type_declaration(str(proposed_replacement["value_type"]))
        with self.transaction() as connection:
            q = self._resolve_question(connection, question)
            entity_id = None
            if example_entity:
                entity = self._resolve_entity(connection, example_entity)
                if entity["kind"] != q["subject_kind"]:
                    raise EpiqError(
                        "challenge_entity_kind",
                        f"Example entity must be a {q['subject_kind']}: {example_entity}",
                    )
                entity_id = str(entity["entity_id"])
            for evidence_id in evidence_ids:
                if not connection.execute(
                    "SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)
                ).fetchone():
                    raise EpiqError("evidence_not_found", f"Evidence not found: {evidence_id}")
            challenge_id = _id("qch")
            payload = {
                "challenge_id": challenge_id,
                "question_id": str(q["question_id"]),
                "problem": problem,
                "explanation": explanation,
                "example_entity_id": entity_id,
                "evidence_ids": evidence_ids,
                "proposed_replacement": proposed_replacement,
            }
            seq, _, _ = self._event(connection, "question.challenge", actor, payload)
            connection.execute(
                "INSERT INTO question_challenges VALUES(?,?,?,?,?,?,'open',NULL,?,NULL)",
                (
                    challenge_id,
                    q["question_id"],
                    problem,
                    explanation,
                    entity_id,
                    _json(proposed_replacement) if proposed_replacement else None,
                    seq,
                ),
            )
            connection.executemany(
                "INSERT INTO question_challenge_evidence VALUES(?,?,?)",
                [
                    (challenge_id, evidence_id, ordinal)
                    for ordinal, evidence_id in enumerate(evidence_ids)
                ],
            )
        return challenge_id

    def resolve_question_challenge(
        self, challenge_id: str, status: str, resolution: str, actor: str
    ) -> None:
        """Resolve or dismiss an open schema challenge through a new event."""
        if status not in {"resolved", "dismissed"}:
            raise EpiqError("invalid_challenge_status", "Status must be resolved or dismissed")
        if not resolution.strip():
            raise EpiqError("invalid_challenge", "Resolution cannot be empty")
        with self.transaction() as connection:
            challenge = connection.execute(
                "SELECT * FROM question_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
            if challenge is None:
                raise EpiqError("challenge_not_found", f"Challenge not found: {challenge_id}")
            if challenge["status"] != "open":
                raise EpiqError(
                    "challenge_closed",
                    f"Challenge is already {challenge['status']}: {challenge_id}",
                )
            seq, _, _ = self._event(
                connection,
                f"question.challenge_{status}",
                actor,
                {
                    "challenge_id": challenge_id,
                    "question_id": str(challenge["question_id"]),
                    "resolution": resolution.strip(),
                },
            )
            connection.execute(
                """UPDATE question_challenges
                   SET status=?,resolution=?,closed_seq=? WHERE challenge_id=?""",
                (status, resolution.strip(), seq, challenge_id),
            )

    def question_challenges(
        self, question: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Return materialized schema challenges with evidence and example context."""
        if status is not None and status not in {"open", "resolved", "dismissed"}:
            raise EpiqError("invalid_challenge_status", f"Unknown challenge status: {status}")
        with self.connect() as connection:
            parameters: list[Any] = []
            clauses = []
            if question:
                q = self._resolve_question(connection, question)
                clauses.append("(qc.question_id=? OR q.name=?)")
                parameters.extend([question, q["name"]])
            if status:
                clauses.append("qc.status=?")
                parameters.append(status)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"""SELECT qc.*,q.name question_name,e.name example_entity_name
                    FROM question_challenges qc
                    JOIN questions q ON q.question_id=qc.question_id
                    LEFT JOIN entities e ON e.entity_id=qc.example_entity_id
                    {where} ORDER BY qc.created_seq""",
                parameters,
            ).fetchall()
            result = []
            for row in rows:
                evidence_ids = [
                    str(item["evidence_id"])
                    for item in connection.execute(
                        """SELECT evidence_id FROM question_challenge_evidence
                           WHERE challenge_id=? ORDER BY ordinal""",
                        (row["challenge_id"],),
                    )
                ]
                result.append(
                    {
                        "challenge_id": str(row["challenge_id"]),
                        "question_id": str(row["question_id"]),
                        "question_name": str(row["question_name"]),
                        "problem": str(row["problem"]),
                        "explanation": str(row["explanation"]),
                        "example_entity_id": row["example_entity_id"],
                        "example_entity_name": row["example_entity_name"],
                        "evidence_ids": evidence_ids,
                        "proposed_replacement": (
                            json.loads(row["proposed_replacement_json"])
                            if row["proposed_replacement_json"]
                            else None
                        ),
                        "status": str(row["status"]),
                        "resolution": row["resolution"],
                    }
                )
            return result

    def season_record(
        self, season: str, known_at: str | None = None, valid_at: str | None = None
    ) -> dict[str, Any]:
        """Derive a season record and its claim-token lineage."""
        cutoff = known_at or "9999-12-31T23:59:59Z"
        valid = valid_at or "9999-12-31"
        with self.connect() as connection:
            season_row = self._resolve_entity(connection, season)
            games = connection.execute("SELECT * FROM entities WHERE kind='Game'").fetchall()
            game_ids = [
                str(row["entity_id"])
                for row in games
                if json.loads(row["attributes_json"]).get("season_id") == season_row["entity_id"]
            ]
            if not game_ids:
                return {
                    "season": season_row["name"],
                    "wins": 0,
                    "losses": 0,
                    "record": "0-0",
                    "lineage": [],
                }
            placeholders = ",".join("?" for _ in game_ids)
            rows = connection.execute(
                f"""SELECT c.*, e.name AS game_name, e.attributes_json
                    FROM claims c JOIN questions q ON q.question_id=c.question_id
                    JOIN entities e ON e.entity_id=c.subject_id
                    WHERE c.subject_id IN ({placeholders}) AND q.name='game_result'
                    AND c.tx_from<=? AND (c.tx_to IS NULL OR c.tx_to>?)
                    AND c.valid_from<=? AND (c.valid_to IS NULL OR c.valid_to>?)
                    AND NOT EXISTS (
                      SELECT 1 FROM claim_validity_ends ve
                      JOIN events vee ON vee.seq=ve.created_seq
                      WHERE ve.claim_id=c.claim_id AND vee.recorded_at<=? AND ve.valid_to<=?
                    )""",
                (*game_ids, cutoff, cutoff, valid, valid, cutoff, valid),
            ).fetchall()
        ordered = sorted(rows, key=lambda row: json.loads(row["attributes_json"]).get("ordinal", 0))
        values = [(row, json.loads(row["value_json"])) for row in ordered]
        wins = sum(value == "W" for _, value in values)
        losses = sum(value == "L" for _, value in values)
        ties = sum(value == "T" for _, value in values)
        return {
            "season": season_row["name"],
            "known_at": known_at,
            "valid_at": valid_at,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "record": f"{wins}-{losses}" + (f"-{ties}" if ties else ""),
            "lineage": [
                {
                    "token": f"p_{row['claim_id']}",
                    "claim_id": row["claim_id"],
                    "game": row["game_name"],
                    "value": value,
                }
                for row, value in values
            ],
        }

    def history(self) -> list[dict[str, Any]]:
        """Return the complete append-only event history."""
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [
            {
                "seq": row["seq"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "recorded_at": row["recorded_at"],
                "actor": row["actor"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def overview(self) -> dict[str, Any]:
        """Describe a project and its available entity projections."""
        with self.connect() as connection:
            meta = {
                str(row["key"]): str(row["value"])
                for row in connection.execute("SELECT key, value FROM meta")
            }
            kinds = connection.execute(
                """SELECT k.kind,
                          COUNT(DISTINCT e.entity_id) AS entities,
                          COUNT(DISTINCT q.name) AS questions
                   FROM entity_kinds k
                   LEFT JOIN entities e ON e.kind=k.kind
                     AND NOT EXISTS (
                       SELECT 1 FROM entity_redirects er WHERE er.from_entity_id=e.entity_id
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM entity_visibility ev
                       WHERE ev.entity_id=e.entity_id AND ev.visible=0
                     )
                   LEFT JOIN questions q ON q.subject_kind=k.kind
                     AND NOT EXISTS (
                       SELECT 1 FROM question_visibility qv
                       WHERE qv.name=q.name AND qv.visible=0
                     )
                   GROUP BY k.kind ORDER BY questions DESC, entities DESC, k.kind"""
            ).fetchall()
        return {
            "project": meta,
            "entity_kinds": [
                {
                    "kind": str(row["kind"]),
                    "entities": int(row["entities"]),
                    "questions": int(row["questions"]),
                }
                for row in kinds
            ],
        }

    def matrix(
        self,
        entity_kind: str,
        question_names: list[str] | None = None,
        known_at: str | None = None,
        valid_at: str | None = None,
    ) -> dict[str, Any]:
        """Project current claims into a general entity-by-question matrix."""
        cutoff = known_at or "9999-12-31T23:59:59Z"
        valid = valid_at or "9999-12-31"
        with self.connect() as connection:
            entities = connection.execute(
                """SELECT * FROM entities e WHERE kind=?
                   AND NOT EXISTS (
                     SELECT 1 FROM entity_redirects r WHERE r.from_entity_id=e.entity_id
                   )
                   AND NOT EXISTS (
                     SELECT 1 FROM entity_visibility v
                     WHERE v.entity_id=e.entity_id AND v.visible=0
                   )
                   ORDER BY name""",
                (entity_kind,),
            ).fetchall()
            if question_names:
                placeholders = ",".join("?" for _ in question_names)
                questions = connection.execute(
                    f"""SELECT q.* FROM questions q
                        JOIN (SELECT name, MAX(version) version, MIN(created_seq) first_seq
                              FROM questions GROUP BY name) latest
                        ON latest.name=q.name AND latest.version=q.version
                        LEFT JOIN question_visibility qv ON qv.name=q.name
                        WHERE q.name IN ({placeholders}) AND COALESCE(qv.visible,1)=1
                        ORDER BY latest.first_seq""",
                    question_names,
                ).fetchall()
            else:
                questions = connection.execute(
                    """SELECT q.* FROM questions q
                       JOIN (SELECT name, MAX(version) version, MIN(created_seq) first_seq
                             FROM questions GROUP BY name) latest
                       ON latest.name=q.name AND latest.version=q.version
                       LEFT JOIN question_visibility qv ON qv.name=q.name
                       WHERE q.subject_kind=? AND COALESCE(qv.visible,1)=1
                       ORDER BY latest.first_seq""",
                    (entity_kind,),
                ).fetchall()
            rows: list[dict[str, Any]] = []
            for entity in entities:
                cells: dict[str, Any] = {}
                entity_ids = self._entity_cluster(connection, str(entity["entity_id"]))
                aliases = connection.execute(
                    f"""SELECT alias FROM entity_aliases
                        WHERE entity_id IN ({",".join("?" for _ in entity_ids)})
                        ORDER BY created_seq""",
                    entity_ids,
                ).fetchall()
                entity_placeholders = ",".join("?" for _ in entity_ids)
                for question in questions:
                    claims = connection.execute(
                        f"""SELECT c.* FROM claims c
                           JOIN questions cq ON cq.question_id=c.question_id
                           WHERE c.subject_id IN ({entity_placeholders}) AND cq.name=?
                           AND c.tx_from<=? AND (c.tx_to IS NULL OR c.tx_to>?)
                           AND c.valid_from<=? AND (c.valid_to IS NULL OR c.valid_to>?)
                           AND NOT EXISTS (
                             SELECT 1 FROM claim_validity_ends ve
                             JOIN events vee ON vee.seq=ve.created_seq
                             WHERE ve.claim_id=c.claim_id AND vee.recorded_at<=? AND ve.valid_to<=?
                           )
                           ORDER BY c.valid_from DESC, c.created_seq DESC""",
                        (
                            *entity_ids,
                            question["name"],
                            cutoff,
                            cutoff,
                            valid,
                            valid,
                            cutoff,
                            valid,
                        ),
                    ).fetchall()
                    definition = json.loads(question["definition_json"])
                    task = connection.execute(
                        f"""SELECT t.* FROM research_tasks t
                           JOIN questions tq ON tq.question_id=t.question_id
                           WHERE t.subject_id IN ({entity_placeholders}) AND tq.name=?
                           ORDER BY t.created_seq DESC LIMIT 1""",
                        (*entity_ids, question["name"]),
                    ).fetchone()
                    cell = self._project_cell(connection, claims, definition, task, cutoff)
                    cells[str(question["name"])] = self._decorate_reference_cell(
                        connection, cell, str(question["value_type"])
                    )
                rows.append(
                    {
                        "entity_id": entity["entity_id"],
                        "name": entity["name"],
                        "aliases": [str(row["alias"]) for row in aliases],
                        "merged_entity_ids": [
                            entity_id
                            for entity_id in entity_ids
                            if entity_id != entity["entity_id"]
                        ],
                        "attributes": json.loads(entity["attributes_json"]),
                        "cells": cells,
                    }
                )
            projected_questions = []
            for question in questions:
                open_challenges = self._open_question_challenges(connection, str(question["name"]))
                projected_questions.append(
                    {
                        "question_id": question["question_id"],
                        "name": question["name"],
                        "value_type": question["value_type"],
                        "definition": json.loads(question["definition_json"]),
                        "schema_state": "challenged" if open_challenges else "active",
                        "open_challenges": open_challenges,
                    }
                )
        return {
            "entity_kind": entity_kind,
            "known_at": known_at,
            "valid_at": valid_at,
            "questions": projected_questions,
            "rows": rows,
        }

    def _decorate_reference_cell(
        self, connection: sqlite3.Connection, cell: dict[str, Any], value_type: str
    ) -> dict[str, Any]:
        if not value_type.startswith("Ref["):
            return cell
        references: list[dict[str, str]] = []
        for value in cell.get("values", []):
            if not isinstance(value, str):
                continue
            entity = self._resolve_entity(connection, value)
            item = {
                "entity_id": str(entity["entity_id"]),
                "name": str(entity["name"]),
                "kind": str(entity["kind"]),
            }
            if item not in references:
                references.append(item)
        cell["references"] = references
        if cell.get("value") is not None and references:
            cell["display_value"] = references[0]
        cell["display_values"] = references
        return cell

    def query_rows(
        self,
        entity_kind: str,
        predicates: list[dict[str, Any]],
        known_at: str | None = None,
        valid_at: str | None = None,
    ) -> dict[str, Any]:
        """Filter a projection with a small, structured, agent-safe predicate language."""
        projection = self.matrix(entity_kind, known_at=known_at, valid_at=valid_at)
        questions = {str(item["name"]): item for item in projection["questions"]}
        allowed = {
            "eq",
            "ne",
            "gt",
            "gte",
            "lt",
            "lte",
            "contains",
            "contains_any",
            "contains_all",
            "any_ref",
            "in",
            "state",
        }
        normalized_predicates = [dict(item) for item in predicates]
        for index, predicate in enumerate(normalized_predicates):
            question = str(predicate.get("question", ""))
            operation = str(predicate.get("op", "eq"))
            if question not in questions:
                raise EpiqError("question_not_found", f"Query predicate {index}: {question}")
            if operation not in allowed:
                raise EpiqError("invalid_query_operator", f"Query predicate {index}: {operation}")
            if "value" not in predicate:
                raise EpiqError("invalid_query_predicate", f"Query predicate {index} needs value")
            value_type = str(questions[question]["value_type"])
            if value_type.startswith("Ref["):
                values = (
                    predicate["value"]
                    if isinstance(predicate["value"], list)
                    else [predicate["value"]]
                )
                with self.connect() as connection:
                    resolved_values = [
                        str(self._resolve_entity(connection, str(value))["entity_id"])
                        for value in values
                    ]
                predicate["value"] = (
                    resolved_values if isinstance(predicate["value"], list) else resolved_values[0]
                )

        def matches(row: dict[str, Any], predicate: dict[str, Any]) -> bool:
            cell = row["cells"][str(predicate["question"])]
            operation = str(predicate.get("op", "eq"))
            expected = predicate["value"]
            if operation == "state":
                return cell["state"] == expected
            actual = cell.get("value")
            if actual is None and cell.get("values"):
                actual = cell["values"]
            try:
                if operation == "eq":
                    return expected in actual if isinstance(actual, list) else actual == expected
                if operation == "ne":
                    return (
                        expected not in actual if isinstance(actual, list) else actual != expected
                    )
                if operation == "gt":
                    return actual > expected
                if operation == "gte":
                    return actual >= expected
                if operation == "lt":
                    return actual < expected
                if operation == "lte":
                    return actual <= expected
                if operation == "contains":
                    return expected in actual
                if operation == "any_ref":
                    return expected in actual if isinstance(actual, list) else actual == expected
                if operation == "contains_any":
                    return any(item in actual for item in expected)
                if operation == "contains_all":
                    return all(item in actual for item in expected)
                if operation == "in":
                    return actual in expected
            except (TypeError, ValueError):
                return False
            return False

        projection["rows"] = [
            row
            for row in projection["rows"]
            if all(matches(row, predicate) for predicate in normalized_predicates)
        ]
        projection["query"] = {
            "predicates": normalized_predicates,
            "matched": len(projection["rows"]),
        }
        return projection

    def dossier(self, entity: str) -> dict[str, Any]:
        """Return a current profile plus all directly attributable history."""
        with self.connect() as connection:
            resolved = self._resolve_entity(connection, entity)
            cluster = self._entity_cluster(connection, str(resolved["entity_id"]))
        projection = self.matrix(str(resolved["kind"]))
        row = next(
            item for item in projection["rows"] if item["entity_id"] == resolved["entity_id"]
        )
        identifiers = set(cluster)
        claim_ids = {
            str(item["claim_id"])
            for cell in row["cells"].values()
            for item in cell.get("lineage", [])
        }
        events = [
            event
            for event in self.history()
            if identifiers.intersection(self._strings(event["payload"]))
            or claim_ids.intersection(self._strings(event["payload"]))
        ]
        return {
            "entity_kind": str(resolved["kind"]),
            "entity": row,
            "questions": projection["questions"],
            "relationships": self.related(str(resolved["entity_id"])),
            "events": events,
        }

    def related(
        self, entity: str, via: str | None = None, direction: str = "both", depth: int = 1
    ) -> dict[str, Any]:
        """Traverse current typed references in either direction."""
        if direction not in {"incoming", "outgoing", "both"}:
            raise EpiqError("invalid_direction", f"Unknown relationship direction: {direction}")
        if depth < 1 or depth > 20:
            raise EpiqError("invalid_depth", "Relationship depth must be between 1 and 20")
        with self.connect() as connection:
            target = self._resolve_entity(connection, entity)
        target_id = str(target["entity_id"])
        graph_edges: list[dict[str, Any]] = []
        overview = self.overview()
        for table in overview["entity_kinds"]:
            projection = self.matrix(str(table["kind"]))
            ref_questions = {
                str(question["name"])
                for question in projection["questions"]
                if str(question["value_type"]).startswith("Ref[")
                and (via is None or question["name"] == via)
            }
            for row in projection["rows"]:
                for question in ref_questions:
                    cell = row["cells"][question]
                    for reference in cell.get("references", []):
                        graph_edges.append(
                            {
                                "question": question,
                                "claim_ids": list(
                                    dict.fromkeys(
                                        str(item["claim_id"])
                                        for item in cell.get("lineage", [])
                                        if item.get("value") == reference["entity_id"]
                                    )
                                ),
                                "from": {
                                    "entity_id": row["entity_id"],
                                    "name": row["name"],
                                    "kind": projection["entity_kind"],
                                },
                                "to": reference,
                            }
                        )
        edges: list[dict[str, Any]] = []
        frontier = {target_id: []}
        visited_entities = {target_id}
        visited_edges: set[tuple[str, str, str, str]] = set()
        for level in range(1, depth + 1):
            next_frontier: dict[str, list[str]] = {}
            for edge in graph_edges:
                candidates: list[tuple[str, str]] = []
                if direction in {"outgoing", "both"} and edge["from"]["entity_id"] in frontier:
                    candidates.append(("outgoing", str(edge["to"]["entity_id"])))
                if direction in {"incoming", "both"} and edge["to"]["entity_id"] in frontier:
                    candidates.append(("incoming", str(edge["from"]["entity_id"])))
                for edge_direction, next_id in candidates:
                    key = (
                        str(edge["from"]["entity_id"]),
                        str(edge["to"]["entity_id"]),
                        str(edge["question"]),
                        edge_direction,
                    )
                    if key in visited_edges:
                        continue
                    visited_edges.add(key)
                    current_id = (
                        str(edge["from"]["entity_id"])
                        if edge_direction == "outgoing"
                        else str(edge["to"]["entity_id"])
                    )
                    path_claim_ids = [*frontier[current_id], *edge["claim_ids"]]
                    edges.append(
                        {
                            "direction": edge_direction,
                            "depth": level,
                            "path_claim_ids": path_claim_ids,
                            **edge,
                        }
                    )
                    if next_id not in visited_entities:
                        next_frontier[next_id] = path_claim_ids
                        visited_entities.add(next_id)
            frontier = next_frontier
            if not frontier:
                break
        return {
            "entity": {
                "entity_id": target_id,
                "name": str(target["name"]),
                "kind": str(target["kind"]),
            },
            "via": via,
            "direction": direction,
            "depth": depth,
            "count": len(edges),
            "edges": edges,
        }

    @staticmethod
    def _strings(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict):
            return set().union(*(Store._strings(item) for item in value.values()), set())
        if isinstance(value, list):
            return set().union(*(Store._strings(item) for item in value), set())
        return set()

    def timeline(self, entity_kind: str, question: str) -> dict[str, Any]:
        """Flatten one field across entities into valid-time chronological observations."""
        projection = self.matrix(entity_kind, [question])
        indexed: dict[str, dict[str, Any]] = {}
        for row in projection["rows"]:
            for lineage in row["cells"][question].get("lineage", []):
                item = indexed.setdefault(
                    str(lineage["claim_id"]),
                    {
                        "entity_id": row["entity_id"],
                        "entity_name": row["name"],
                        "claim_id": lineage["claim_id"],
                        "value": lineage["value"],
                        "as_of": lineage["as_of"],
                        "confidence": lineage["confidence"],
                        "sources": [],
                    },
                )
                item["sources"].append({"evidence_id": lineage["evidence_id"], **lineage["source"]})
        observations = list(indexed.values())
        observations.sort(key=lambda item: (item["as_of"], item["entity_name"], item["claim_id"]))
        return {"entity_kind": entity_kind, "question": question, "observations": observations}

    def delta_report(self, actor: str, since_seq: int | None = None) -> dict[str, Any]:
        """Return events since an explicit or prior-report baseline and record the new baseline."""
        with self.transaction() as connection:
            if since_seq is None:
                prior = connection.execute(
                    """SELECT payload_json FROM events
                       WHERE event_type='report.generated' ORDER BY seq DESC LIMIT 1"""
                ).fetchone()
                since_seq = int(json.loads(prior["payload_json"])["through_seq"]) if prior else 0
            if since_seq < 0:
                raise EpiqError("invalid_baseline", "Delta baseline cannot be negative")
            through_seq = int(
                connection.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            )
            rows = connection.execute(
                """SELECT * FROM events WHERE seq>? AND seq<=? AND event_type<>'report.generated'
                   ORDER BY seq""",
                (since_seq, through_seq),
            ).fetchall()
            events = [
                {
                    "seq": int(row["seq"]),
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "recorded_at": str(row["recorded_at"]),
                    "actor": str(row["actor"]),
                    "payload": json.loads(str(row["payload_json"])),
                }
                for row in rows
            ]
            digest = hashlib.sha256(_json(events).encode()).hexdigest()
            _, report_id, _ = self._event(
                connection,
                "report.generated",
                actor,
                {
                    "report_type": "delta",
                    "since_seq": since_seq,
                    "through_seq": through_seq,
                    "event_count": len(events),
                    "content_sha256": digest,
                },
            )
        return {
            "report_id": report_id,
            "since_seq": since_seq,
            "through_seq": through_seq,
            "event_count": len(events),
            "content_sha256": digest,
            "events": events,
        }

    @staticmethod
    def _open_question_challenges(
        connection: sqlite3.Connection, question_name: str
    ) -> list[dict[str, str]]:
        rows = connection.execute(
            """SELECT qc.challenge_id,qc.problem,qc.explanation
               FROM question_challenges qc
               JOIN questions q ON q.question_id=qc.question_id
               WHERE q.name=? AND qc.status='open' ORDER BY qc.created_seq""",
            (question_name,),
        ).fetchall()
        return [
            {
                "challenge_id": str(row["challenge_id"]),
                "problem": str(row["problem"]),
                "explanation": str(row["explanation"]),
            }
            for row in rows
        ]

    def _project_cell(
        self,
        connection: sqlite3.Connection,
        claims: list[sqlite3.Row],
        definition: dict[str, Any],
        task: sqlite3.Row | None = None,
        cutoff: str = "9999-12-31T23:59:59Z",
    ) -> dict[str, Any]:
        if not claims:
            if task and task["status"] == "not_found":
                return {
                    "state": "NotFound",
                    "values": [],
                    "lineage": [],
                    "research": {
                        "task_id": task["task_id"],
                        "query": task["query"],
                        "notes": task["notes"],
                    },
                }
            return {"state": "Unasked", "values": [], "lineage": []}
        values = [json.loads(claim["value_json"]) for claim in claims]
        unique = {_json(value) for value in values}
        cardinality = definition.get("cardinality", "one")
        if cardinality == "many":
            state = "Answered"
        elif len(unique) > 1:
            state = "Contested"
        else:
            state = "Answered"
        lineage: list[dict[str, Any]] = []
        for claim in claims:
            evidence_rows = connection.execute(
                """SELECT ce.evidence_id,e.excerpt,s.url,s.source_type,s.title,
                          s.published_at,s.retrieved_at,source_event.actor evidence_actor,
                          source_event.payload_json evidence_payload,s.locator_json,
                          s.linked_entity_id
                   FROM claim_evidence ce
                   JOIN evidence e ON e.evidence_id=ce.evidence_id
                   JOIN sources s ON s.source_id=e.source_id
                   JOIN events source_event ON source_event.seq=e.created_seq
                   JOIN events ev ON ev.seq=ce.created_seq
                   JOIN claims owner ON owner.claim_id=ce.claim_id
                   WHERE ce.claim_id=? AND (ce.created_seq=owner.created_seq OR ev.recorded_at<=?)
                   ORDER BY ce.ordinal""",
                (claim["claim_id"], cutoff),
            ).fetchall()
            derivation = connection.execute(
                "SELECT * FROM derivations WHERE claim_id=?", (claim["claim_id"],)
            ).fetchone()
            claim_event = connection.execute(
                "SELECT actor,payload_json FROM events WHERE seq=?", (claim["created_seq"],)
            ).fetchone()
            claim_payload = json.loads(str(claim_event["payload_json"]))
            input_claim_ids = [
                str(row["input_claim_id"])
                for row in connection.execute(
                    """SELECT ci.input_claim_id FROM claim_inputs ci
                       JOIN events ev ON ev.seq=ci.created_seq
                       JOIN claims owner ON owner.claim_id=ci.claim_id
                       WHERE ci.claim_id=?
                       AND (ci.created_seq=owner.created_seq OR ev.recorded_at<=?)
                       ORDER BY ci.ordinal""",
                    (claim["claim_id"], cutoff),
                )
            ]
            dependencies = [
                {
                    "claim_id": str(row["dependency_claim_id"]),
                    "role": str(row["role"]),
                }
                for row in connection.execute(
                    """SELECT dependency_claim_id,role FROM derivation_dependencies
                       WHERE claim_id=? ORDER BY role,ordinal""",
                    (claim["claim_id"],),
                )
            ]
            for evidence_row in evidence_rows:
                assessment = connection.execute(
                    """SELECT a.status,a.reason FROM evidence_assessments a
                       JOIN events ev ON ev.seq=a.created_seq
                       WHERE a.evidence_id=? AND ev.recorded_at<=?
                       ORDER BY a.created_seq DESC LIMIT 1""",
                    (evidence_row["evidence_id"], cutoff),
                ).fetchone()
                item = {
                    "token": f"p_{claim['claim_id']}",
                    "claim_id": claim["claim_id"],
                    "value": json.loads(claim["value_json"]),
                    "confidence": claim["confidence"],
                    "actor": str(claim_event["actor"]),
                    "submitted_by": claim_payload.get("submitted_by"),
                    "evidence_id": evidence_row["evidence_id"],
                    "source": {
                        "title": evidence_row["title"],
                        "url": evidence_row["url"],
                        "source_type": evidence_row["source_type"],
                        "actor": evidence_row["evidence_actor"],
                        "submitted_by": json.loads(str(evidence_row["evidence_payload"])).get(
                            "submitted_by"
                        ),
                        "published_at": evidence_row["published_at"],
                        "retrieved_at": evidence_row["retrieved_at"],
                        "locator": json.loads(str(evidence_row["locator_json"])),
                        "linked_entity_id": evidence_row["linked_entity_id"],
                    },
                    "as_of": claim["valid_from"],
                    "temporal_basis": claim["temporal_basis"],
                    "excerpt": evidence_row["excerpt"],
                    "evidence_status": str(assessment["status"]) if assessment else "unassessed",
                    "evidence_assessment_reason": (
                        str(assessment["reason"]) if assessment else None
                    ),
                }
                if derivation:
                    item["derivation"] = {
                        "operation": derivation["operation"],
                        "parameters": json.loads(derivation["parameters_json"]),
                        "input_claim_ids": input_claim_ids,
                        "dependencies": dependencies,
                    }
                lineage.append(item)
        volatility = definition.get("volatility", "stable")
        freshness_days = definition.get("freshness_days")
        newest_as_of = max((str(claim["valid_from"]) for claim in claims), default=None)
        freshness = "not_applicable" if volatility == "stable" else "unknown"
        age_days = None
        basis = str(claims[0]["temporal_basis"]) if claims else "unknown"
        if (
            volatility != "stable"
            and newest_as_of
            and basis != "unknown"
            and isinstance(freshness_days, int)
        ):
            try:
                age_days = (date.today() - date.fromisoformat(newest_as_of[:10])).days
                freshness = "fresh" if age_days <= freshness_days else "stale"
            except ValueError:
                freshness = "unknown"
        return {
            "state": state,
            "value": values[0] if cardinality == "one" and len(unique) == 1 else None,
            "values": values,
            "confidence": claims[0]["confidence"] if len(claims) == 1 else None,
            "lineage": lineage,
            "temporal": {
                "volatility": volatility,
                "freshness_days": freshness_days,
                "as_of": newest_as_of,
                "age_days": age_days,
                "freshness": freshness,
                "basis": basis,
            },
        }

    def record_not_found(
        self, subject: str, question: str, query: str, notes: str, actor: str
    ) -> str:
        """Record a completed research act that found no sufficient evidence."""
        with self.transaction() as connection:
            entity = self._resolve_entity(connection, subject)
            q = self._resolve_question(connection, question)
            task_id = _id("tsk")
            payload = {
                "task_id": task_id,
                "subject_id": entity["entity_id"],
                "question_id": q["question_id"],
                "query": query,
                "notes": notes,
                "outcome": "not_found",
            }
            seq, _, _ = self._event(connection, "research.not_found", actor, payload)
            connection.execute(
                """INSERT INTO research_tasks
                   (task_id,subject_id,question_id,status,query,notes,created_seq,closed_seq)
                   VALUES(?,?,?,'not_found',?,?,?,?)""",
                (task_id, entity["entity_id"], q["question_id"], query, notes, seq, seq),
            )
            return task_id

    def record_research_feedback(
        self, task_id: str, reason: str, research_guidance: str, actor: str
    ) -> dict[str, str]:
        """Challenge how an unsuccessful research outcome was interpreted."""
        if not reason.strip():
            raise EpiqError("invalid_research_feedback", "Feedback reason cannot be empty")
        with self.transaction() as connection:
            task = connection.execute(
                "SELECT * FROM research_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                raise EpiqError("research_task_not_found", f"Research task not found: {task_id}")
            self._event(
                connection,
                "research.feedback",
                actor,
                {
                    "task_id": task_id,
                    "subject_id": str(task["subject_id"]),
                    "question_id": str(task["question_id"]),
                    "reason": reason.strip(),
                    "research_guidance": research_guidance.strip(),
                },
            )
            return {
                "task_id": task_id,
                "subject_id": str(task["subject_id"]),
                "question_id": str(task["question_id"]),
            }
