"""SQLite connection ownership and serialized, atomic schema upgrades."""

from __future__ import annotations

import sqlite3
from time import monotonic, sleep

from .errors import EpiqError

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


class Connection(sqlite3.Connection):
    """Commit/rollback and close when used as a context manager."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def enable_wal(connection: sqlite3.Connection) -> None:
    """Handle competing first opens before the database has entered WAL mode."""
    deadline = monotonic() + 10
    while True:
        try:
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or monotonic() >= deadline:
                raise
            sleep(0.01)


def _execute_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute SQL without executescript's implicit pre-transaction commit."""
    statement = ""
    for char in script:
        statement += char
        if char == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL")


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise EpiqError("invalid_project", "Database has no schema_version metadata")
    version = int(row[0])
    if version > LATEST_SCHEMA_VERSION:
        raise EpiqError(
            "schema_too_new",
            f"Database schema v{version} is newer than supported v{LATEST_SCHEMA_VERSION}",
        )
    return version


def migrate(connection: sqlite3.Connection) -> None:
    """Recheck the version under SQLite's cross-process writer lock, then upgrade."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        if schema_version(connection) < LATEST_SCHEMA_VERSION:
            _upgrade(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _upgrade(connection: sqlite3.Connection) -> None:
    _execute_script(
        connection,
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
        """,
    )
    source_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(sources)")}
    if "published_at" not in source_columns:
        connection.execute("ALTER TABLE sources ADD COLUMN published_at TEXT")
    if "source_type" not in source_columns:
        connection.execute("ALTER TABLE sources ADD COLUMN source_type TEXT NOT NULL DEFAULT 'web'")
    claim_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(claims)")}
    if "temporal_basis" not in claim_columns:
        connection.execute(
            "ALTER TABLE claims ADD COLUMN temporal_basis TEXT NOT NULL DEFAULT 'unknown'"
        )
    connection.execute(
        "UPDATE meta SET value='3' WHERE key='schema_version' AND CAST(value AS INTEGER)<3"
    )
    _execute_script(
        connection,
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
        """,
    )
    entity_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(entities)")}
    if "role" not in entity_columns:
        connection.execute("ALTER TABLE entities ADD COLUMN role TEXT NOT NULL DEFAULT 'entity'")
    if "identity_json" not in entity_columns:
        connection.execute("ALTER TABLE entities ADD COLUMN identity_json TEXT")
    if "identity_hash" not in entity_columns:
        connection.execute("ALTER TABLE entities ADD COLUMN identity_hash TEXT")
    source_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(sources)")}
    if "locator_json" not in source_columns:
        connection.execute("ALTER TABLE sources ADD COLUMN locator_json TEXT NOT NULL DEFAULT '{}'")
    if "linked_entity_id" not in source_columns:
        connection.execute("ALTER TABLE sources ADD COLUMN linked_entity_id TEXT")
    _execute_script(
        connection,
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
        """,
    )
