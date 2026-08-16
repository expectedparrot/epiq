"""Transactional SQLite storage for Epiq's append-only epistemic history."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import EpiqError

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

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
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
"""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
                    ("schema_version", "2"),
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
        return connection

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
        self, url: str, title: str, retrieved_at: str, excerpt: str, actor: str
    ) -> tuple[str, str]:
        """Atomically add a source and an immutable evidence fragment."""
        source_hash = hashlib.sha256(f"{url}\n{excerpt}".encode()).hexdigest()
        evidence_hash = hashlib.sha256(f"{source_hash}\n{excerpt}".encode()).hexdigest()
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
                    "url": url,
                    "title": title,
                    "retrieved_at": retrieved_at,
                    "excerpt": excerpt,
                    "content_hash": evidence_hash,
                },
            )
            connection.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?,?)",
                (source_id, url, title, retrieved_at, source_hash, seq),
            )
            connection.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?)",
                (evidence_id, source_id, excerpt, evidence_hash, seq),
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
        event_type: str = "claim.assert",
        extra_payload: dict[str, Any] | None = None,
    ) -> tuple[str, int, bool]:
        entity = self._resolve_entity(connection, subject)
        q = self._resolve_question(connection, question)
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
            **(extra_payload or {}),
        }
        seq, _, event_time = self._event(connection, event_type, actor, payload)
        tx_from = recorded_at or event_time
        connection.execute(
            """INSERT INTO claims
               (claim_id,subject_id,question_id,value_json,valid_from,valid_to,tx_from,tx_to,
                evidence_id,confidence,status,created_seq,closed_seq)
               VALUES(?,?,?,?,?,NULL,?,NULL,?,?,'asserted',?,NULL)""",
            (
                claim_id,
                entity["entity_id"],
                q["question_id"],
                value_json,
                valid_from,
                tx_from,
                primary_evidence,
                confidence,
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
                """SELECT e.kind, COUNT(DISTINCT e.entity_id) AS entities,
                          COUNT(DISTINCT q.question_id) AS questions
                   FROM entities e
                   LEFT JOIN questions q ON q.subject_kind=e.kind
                   GROUP BY e.kind ORDER BY entities DESC, e.kind"""
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
                        JOIN (SELECT name, MAX(version) version FROM questions GROUP BY name) latest
                        ON latest.name=q.name AND latest.version=q.version
                        WHERE q.name IN ({placeholders}) ORDER BY q.name""",
                    question_names,
                ).fetchall()
            else:
                questions = connection.execute(
                    """SELECT q.* FROM questions q
                       JOIN (SELECT name, MAX(version) version FROM questions GROUP BY name) latest
                       ON latest.name=q.name AND latest.version=q.version
                       WHERE q.subject_kind=? ORDER BY q.name""",
                    (entity_kind,),
                ).fetchall()
            rows: list[dict[str, Any]] = []
            for entity in entities:
                cells: dict[str, Any] = {}
                for question in questions:
                    claims = connection.execute(
                        """SELECT c.* FROM claims c
                           WHERE c.subject_id=? AND c.question_id=?
                           AND c.tx_from<=? AND (c.tx_to IS NULL OR c.tx_to>?)
                           AND c.valid_from<=? AND (c.valid_to IS NULL OR c.valid_to>?)
                           ORDER BY c.valid_from DESC, c.created_seq DESC""",
                        (
                            entity["entity_id"],
                            question["question_id"],
                            cutoff,
                            cutoff,
                            valid,
                            valid,
                        ),
                    ).fetchall()
                    definition = json.loads(question["definition_json"])
                    task = connection.execute(
                        """SELECT t.* FROM research_tasks t
                           WHERE t.subject_id=? AND t.question_id=?
                           ORDER BY t.created_seq DESC LIMIT 1""",
                        (entity["entity_id"], question["question_id"]),
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
        return {
            "entity_kind": entity_kind,
            "known_at": known_at,
            "valid_at": valid_at,
            "questions": [
                {
                    "question_id": q["question_id"],
                    "name": q["name"],
                    "value_type": q["value_type"],
                    "definition": json.loads(q["definition_json"]),
                }
                for q in questions
            ],
            "rows": rows,
        }

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
                """SELECT ce.evidence_id,e.excerpt,s.url,s.title
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
                    "source": {"title": evidence_row["title"], "url": evidence_row["url"]},
                    "excerpt": evidence_row["excerpt"],
                }
                if derivation:
                    item["derivation"] = {
                        "operation": derivation["operation"],
                        "parameters": json.loads(derivation["parameters_json"]),
                        "input_claim_ids": input_claim_ids,
                    }
                lineage.append(item)
        return {
            "state": state,
            "value": values[0] if cardinality == "one" and len(unique) == 1 else None,
            "values": values,
            "confidence": claims[0]["confidence"] if len(claims) == 1 else None,
            "lineage": lineage,
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
