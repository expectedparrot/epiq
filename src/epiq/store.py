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
    created_seq INTEGER NOT NULL REFERENCES events(seq),
    UNIQUE(kind, name)
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

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    published_at TEXT,
    content_hash TEXT NOT NULL,
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
                    ("schema_version", "6"),
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS entity_kinds (
                    kind TEXT PRIMARY KEY,
                    created_seq INTEGER NOT NULL REFERENCES events(seq)
                );
                INSERT OR IGNORE INTO entity_kinds(kind,created_seq)
                SELECT kind,MIN(created_seq) FROM entities GROUP BY kind;
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
                SELECT claim_id,evidence_id,0,created_seq FROM claims;
                UPDATE meta SET value='2' WHERE key='schema_version' AND CAST(value AS INTEGER)<2;
                """
            )
            source_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(sources)")
            }
            if "published_at" not in source_columns:
                connection.execute("ALTER TABLE sources ADD COLUMN published_at TEXT")
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
                """
            )
            connection.commit()
        return connection

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
                    "questions",
                    "question_visibility",
                    "sources",
                    "evidence",
                    "claims",
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
        self, kind: str, name: str, attributes: dict[str, Any] | None, actor: str
    ) -> str:
        """Add an entity through an event."""
        entity_id = _id("ent")
        payload = {
            "entity_id": entity_id,
            "kind": kind,
            "name": name,
            "attributes": attributes or {},
        }
        try:
            with self.transaction() as connection:
                seq, _, _ = self._event(connection, "entity.create", actor, payload)
                connection.execute("INSERT OR IGNORE INTO entity_kinds VALUES(?,?)", (kind, seq))
                connection.execute(
                    "INSERT INTO entities VALUES(?,?,?,?,?)",
                    (entity_id, kind, name, _json(attributes or {}), seq),
                )
        except sqlite3.IntegrityError as exc:
            raise EpiqError("duplicate_entity", f"Entity already exists: {kind} {name}") from exc
        return entity_id

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
            connection.execute(
                "INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
                (question_id, name, version, subject_kind, value_type, _json(definition), seq),
            )
        return question_id

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
        if value_type in {"Int", "Float", "Probability", "Bool", "String", "Json"}:
            return
        if value_type.startswith("Enum[") and value_type.endswith("]"):
            if all(part.strip() for part in value_type[5:-1].split(",")):
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
    ) -> tuple[str, str]:
        """Atomically add a source and an immutable evidence fragment."""
        canonical_url = canonicalize_url(url)
        normalized_excerpt = _normalize_excerpt(excerpt)
        if not normalized_excerpt:
            raise EpiqError("invalid_evidence", "Evidence excerpt cannot be empty")
        source_hash = hashlib.sha256(f"{canonical_url}\n{normalized_excerpt}".encode()).hexdigest()
        evidence_hash = hashlib.sha256(
            f"{canonical_url}\n{normalized_excerpt}".encode()
        ).hexdigest()
        with self.transaction() as connection:
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
                    "title": title,
                    "retrieved_at": retrieved_at,
                    "published_at": published_at,
                    "excerpt": normalized_excerpt,
                    "content_hash": evidence_hash,
                },
            )
            connection.execute(
                """INSERT INTO sources
                   (source_id,url,title,retrieved_at,published_at,content_hash,created_seq)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    source_id,
                    canonical_url,
                    title,
                    retrieved_at,
                    published_at,
                    source_hash,
                    seq,
                ),
            )
            connection.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?)",
                (evidence_id, source_id, normalized_excerpt, evidence_hash, seq),
            )
        return source_id, evidence_id

    def _resolve_entity(self, connection: sqlite3.Connection, reference: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM entities WHERE entity_id=? OR name=?", (reference, reference)
        ).fetchone()
        if not row:
            raise EpiqError("entity_not_found", f"Entity not found: {reference}")
        return row

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
        entity = self._resolve_entity(connection, subject)
        q = self._resolve_question(connection, question)
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
        self._check_value_type(str(q["value_type"]), value)
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
            return claim_id

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
        elif value_type not in {"Int", "Float", "Bool", "String", "Json", "Probability"}:
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
                    AND c.valid_from<=? AND (c.valid_to IS NULL OR c.valid_to>?)""",
                (*game_ids, cutoff, cutoff, valid, valid),
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
                "SELECT * FROM entities WHERE kind=? ORDER BY name", (entity_kind,)
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
                for question in questions:
                    claims = connection.execute(
                        """SELECT c.* FROM claims c
                           JOIN questions cq ON cq.question_id=c.question_id
                           WHERE c.subject_id=? AND cq.name=?
                           AND c.tx_from<=? AND (c.tx_to IS NULL OR c.tx_to>?)
                           AND c.valid_from<=? AND (c.valid_to IS NULL OR c.valid_to>?)
                           ORDER BY c.valid_from DESC, c.created_seq DESC""",
                        (
                            entity["entity_id"],
                            question["name"],
                            cutoff,
                            cutoff,
                            valid,
                            valid,
                        ),
                    ).fetchall()
                    definition = json.loads(question["definition_json"])
                    task = connection.execute(
                        """SELECT t.* FROM research_tasks t
                           JOIN questions tq ON tq.question_id=t.question_id
                           WHERE t.subject_id=? AND tq.name=?
                           ORDER BY t.created_seq DESC LIMIT 1""",
                        (entity["entity_id"], question["name"]),
                    ).fetchone()
                    cells[str(question["name"])] = self._project_cell(
                        connection, claims, definition, task, cutoff
                    )
                rows.append(
                    {
                        "entity_id": entity["entity_id"],
                        "name": entity["name"],
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
                """SELECT ce.evidence_id,e.excerpt,s.url,s.title,s.published_at,s.retrieved_at
                   FROM claim_evidence ce
                   JOIN evidence e ON e.evidence_id=ce.evidence_id
                   JOIN sources s ON s.source_id=e.source_id
                   JOIN events ev ON ev.seq=ce.created_seq
                   JOIN claims owner ON owner.claim_id=ce.claim_id
                   WHERE ce.claim_id=? AND (ce.created_seq=owner.created_seq OR ev.recorded_at<=?)
                   ORDER BY ce.ordinal""",
                (claim["claim_id"], cutoff),
            ).fetchall()
            derivation = connection.execute(
                "SELECT * FROM derivations WHERE claim_id=?", (claim["claim_id"],)
            ).fetchone()
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
            for evidence_row in evidence_rows:
                item = {
                    "token": f"p_{claim['claim_id']}",
                    "claim_id": claim["claim_id"],
                    "value": json.loads(claim["value_json"]),
                    "confidence": claim["confidence"],
                    "evidence_id": evidence_row["evidence_id"],
                    "source": {
                        "title": evidence_row["title"],
                        "url": evidence_row["url"],
                        "published_at": evidence_row["published_at"],
                        "retrieved_at": evidence_row["retrieved_at"],
                    },
                    "as_of": claim["valid_from"],
                    "temporal_basis": claim["temporal_basis"],
                    "excerpt": evidence_row["excerpt"],
                }
                if derivation:
                    item["derivation"] = {
                        "operation": derivation["operation"],
                        "parameters": json.loads(derivation["parameters_json"]),
                        "input_claim_ids": input_claim_ids,
                    }
                lineage.append(item)
        volatility = definition.get("volatility", "stable")
        freshness_days = definition.get("freshness_days")
        newest_as_of = max((str(claim["valid_from"]) for claim in claims), default=None)
        freshness = "not_applicable" if volatility == "stable" else "unknown"
        age_days = None
        basis = str(claims[0]["temporal_basis"]) if claims else "unknown"
        if volatility != "stable" and newest_as_of and basis != "unknown":
            try:
                age_days = (date.today() - date.fromisoformat(newest_as_of[:10])).days
                freshness = (
                    "fresh"
                    if isinstance(freshness_days, int) and age_days <= freshness_days
                    else "stale"
                )
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
