"""Read models over the disposable projection of a Git-native Epiq ledger."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from .ledger import LedgerRepository
from .ledger_projection import LedgerProjector


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class LedgerView:
    """JSON-ready, lossless reads backed by a catch-up projection."""

    def __init__(self, repository: LedgerRepository) -> None:
        self.repository = repository
        self.projector = LedgerProjector(repository)

    def _connect(self) -> sqlite3.Connection:
        self.projector.build()
        connection = sqlite3.connect(self.projector.path)
        connection.row_factory = sqlite3.Row
        return connection

    def project(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT project_id,name,format_version FROM projected_project"
            ).fetchone()
        return dict(row)

    def kinds(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT kind FROM projected_entity_kinds ORDER BY kind"
            ).fetchall()
        return [str(row["kind"]) for row in rows]

    def entities(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT e.entity_id,e.kind,e.name,e.attributes_json,e.role,e.identity_json,
                   CASE WHEN state.entity_id IS NULL THEN 1 ELSE state.visible END AS visible,
                   COALESCE(state.conflicted,0) AS visibility_conflicted
            FROM projected_entities e
            LEFT JOIN projected_entity_visibility_state state USING(entity_id)
        """
        parameters: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE e.kind=?"
            parameters = (kind,)
        query += " ORDER BY e.kind,e.name_key,e.entity_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "entity_id": row["entity_id"],
                "kind": row["kind"],
                "name": row["name"],
                "attributes": _decode(row["attributes_json"]),
                "role": row["role"],
                "identity": _decode(row["identity_json"]),
                "visible": None if row["visible"] is None else bool(row["visible"]),
                "visibility_conflicted": bool(row["visibility_conflicted"]),
            }
            for row in rows
        ]

    def questions(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT q.question_id,q.name,q.subject_kind,q.value_type,q.definition_json,
                   CASE WHEN state.question_id IS NULL THEN 1 ELSE state.visible END AS visible,
                   COALESCE(state.conflicted,0) AS visibility_conflicted
            FROM projected_questions q
            LEFT JOIN projected_question_visibility_state state USING(question_id)
        """
        parameters: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE q.subject_kind=?"
            parameters = (kind,)
        query += " ORDER BY q.subject_kind,q.name,q.question_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "question_id": row["question_id"],
                "name": row["name"],
                "subject_kind": row["subject_kind"],
                "value_type": row["value_type"],
                "definition": _decode(row["definition_json"]),
                "visible": None if row["visible"] is None else bool(row["visible"]),
                "visibility_conflicted": bool(row["visibility_conflicted"]),
            }
            for row in rows
        ]

    def evidence(self, *, evidence_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        identifiers = sorted(set(evidence_ids or ()))
        query = """
            SELECT evidence_id,source_id,url,source_type,title,retrieved_at,published_at,
                   excerpt,content_hash,locator_json,source_entity_id,capture_json
            FROM projected_evidence
        """
        parameters: tuple[str, ...] = ()
        if identifiers:
            placeholders = ",".join("?" for _ in identifiers)
            query += f" WHERE evidence_id IN ({placeholders})"
            parameters = tuple(identifiers)
        query += " ORDER BY retrieved_at,evidence_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "evidence_id": row["evidence_id"],
                "source_id": row["source_id"],
                "url": row["url"],
                "source_type": row["source_type"],
                "title": row["title"],
                "retrieved_at": row["retrieved_at"],
                "published_at": row["published_at"],
                "excerpt": row["excerpt"],
                "content_hash": row["content_hash"],
                "locator": _decode(row["locator_json"]),
                "source_entity_id": row["source_entity_id"],
                "capture": _decode(row["capture_json"]),
            }
            for row in rows
        ]

    def claims(
        self,
        *,
        kind: str | None = None,
        question: str | None = None,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        table = "projected_claims" if include_closed else "projected_active_claims"
        query = f"""
            SELECT c.claim_id,c.subject_id,e.name AS subject_name,e.kind,
                   c.question_id,q.name AS question_name,c.value_json,c.valid_from,
                   c.confidence,c.temporal_basis,c.transaction_id,t.actor,t.recorded_at
            FROM {table} c
            JOIN projected_entities e ON e.entity_id=c.subject_id
            JOIN projected_questions q ON q.question_id=c.question_id
            JOIN ledger_transactions t ON t.transaction_id=c.transaction_id
        """
        clauses: list[str] = []
        parameters: list[str] = []
        if kind is not None:
            clauses.append("e.kind=?")
            parameters.append(kind)
        if question is not None:
            clauses.append("(q.question_id=? OR q.name=?)")
            parameters.extend((question, question))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY e.kind,e.name_key,q.name,c.valid_from,c.claim_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            claim_ids = [str(row["claim_id"]) for row in rows]
            supports = self._supports(connection, claim_ids)
            reviews = self._reviews(connection, claim_ids)
        return [
            {
                "claim_id": row["claim_id"],
                "subject_id": row["subject_id"],
                "subject_name": row["subject_name"],
                "kind": row["kind"],
                "question_id": row["question_id"],
                "question_name": row["question_name"],
                "value": _decode(row["value_json"]),
                "valid_from": row["valid_from"],
                "confidence": row["confidence"],
                "temporal_basis": row["temporal_basis"],
                "transaction_id": row["transaction_id"],
                "actor": row["actor"],
                "recorded_at": row["recorded_at"],
                "evidence_ids": supports.get(str(row["claim_id"]), []),
                "reviews": reviews.get(str(row["claim_id"]), []),
            }
            for row in rows
        ]

    def matrix(self, kind: str) -> dict[str, Any]:
        entities = [item for item in self.entities(kind=kind) if item["visible"] is True]
        questions = [item for item in self.questions(kind=kind) if item["visible"] is True]
        claims = self.claims(kind=kind)
        cells: dict[str, dict[str, list[dict[str, Any]]]] = {
            item["entity_id"]: {question["question_id"]: [] for question in questions}
            for item in entities
        }
        for claim in claims:
            if claim["subject_id"] in cells and claim["question_id"] in cells[claim["subject_id"]]:
                cells[claim["subject_id"]][claim["question_id"]].append(claim)
        return {
            "kind": kind,
            "entities": entities,
            "questions": questions,
            "cells": cells,
        }

    def conflicts(self, *, conflict_type: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT conflict_id,conflict_type,object_ids_json,detail_json
            FROM projected_conflicts
        """
        parameters: tuple[str, ...] = ()
        if conflict_type is not None:
            query += " WHERE conflict_type=?"
            parameters = (conflict_type,)
        query += " ORDER BY conflict_type,conflict_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "conflict_id": row["conflict_id"],
                "conflict_type": row["conflict_type"],
                "object_ids": _decode(row["object_ids_json"]),
                "detail": _decode(row["detail_json"]),
            }
            for row in rows
        ]

    def question_challenges(
        self, *, question: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = """
            SELECT challenge.challenge_id,challenge.question_id,q.name AS question_name,
                   challenge.problem,challenge.explanation,challenge.example_entity_id,
                   entity.name AS example_entity_name,challenge.evidence_ids_json,
                   challenge.proposed_replacement_json
            FROM projected_question_challenges challenge
            JOIN projected_questions q ON q.question_id=challenge.question_id
            LEFT JOIN projected_entities entity
              ON entity.entity_id=challenge.example_entity_id
        """
        parameters: list[str] = []
        if question is not None:
            query += " WHERE (challenge.question_id=? OR q.name=?)"
            parameters.extend((question, question))
        query += " ORDER BY challenge.challenge_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            resolution_rows: dict[str, list[tuple[str, str]]] = {}
            for resolution in connection.execute(
                """SELECT challenge_id,status,resolution
                   FROM projected_question_challenge_resolutions
                   ORDER BY challenge_id,resolution_id"""
            ):
                resolution_rows.setdefault(str(resolution["challenge_id"]), []).append(
                    (str(resolution["status"]), str(resolution["resolution"]))
                )
            resolutions: dict[str, dict[str, Any]] = {}
            for challenge_id, resolution_items in resolution_rows.items():
                decisions = sorted(set(resolution_items))
                resolutions[challenge_id] = (
                    {
                        "status": decisions[0][0],
                        "resolution": decisions[0][1],
                        "resolution_conflicted": False,
                    }
                    if len(decisions) == 1
                    else {
                        "status": "open",
                        "resolution": None,
                        "resolution_conflicted": True,
                    }
                )
        items = [
            {
                "challenge_id": row["challenge_id"],
                "question_id": row["question_id"],
                "question_name": row["question_name"],
                "problem": row["problem"],
                "explanation": row["explanation"],
                "example_entity_id": row["example_entity_id"],
                "example_entity_name": row["example_entity_name"],
                "evidence_ids": _decode(row["evidence_ids_json"]),
                "proposed_replacement": _decode(row["proposed_replacement_json"]),
                **resolutions.get(
                    str(row["challenge_id"]),
                    {
                        "status": "open",
                        "resolution": None,
                        "resolution_conflicted": False,
                    },
                ),
            }
            for row in rows
        ]
        return [item for item in items if status is None or item["status"] == status]

    def research_outcomes(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT outcome.outcome_id,outcome.subject_id,entity.name AS subject_name,
                   entity.kind,outcome.question_id,question.name AS question_name,
                   outcome.query,outcome.notes,outcome.transaction_id
            FROM projected_research_outcomes outcome
            JOIN projected_entities entity ON entity.entity_id=outcome.subject_id
            JOIN projected_questions question ON question.question_id=outcome.question_id
        """
        parameters: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE entity.kind=?"
            parameters = (kind,)
        query += " ORDER BY entity.name_key,question.name,outcome.outcome_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _supports(
        connection: sqlite3.Connection, claim_ids: list[str]
    ) -> dict[str, list[str]]:
        if not claim_ids:
            return {}
        placeholders = ",".join("?" for _ in claim_ids)
        rows = connection.execute(
            "SELECT claim_id,evidence_id FROM projected_claim_supports "
            f"WHERE claim_id IN ({placeholders}) ORDER BY claim_id,evidence_id",
            claim_ids,
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            evidence = result.setdefault(str(row["claim_id"]), [])
            identifier = str(row["evidence_id"])
            if identifier not in evidence:
                evidence.append(identifier)
        return result

    @staticmethod
    def _reviews(
        connection: sqlite3.Connection, claim_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not claim_ids:
            return {}
        placeholders = ",".join("?" for _ in claim_ids)
        rows = connection.execute(
            "SELECT review_id,claim_id,decision,reason,transaction_id "
            f"FROM projected_claim_reviews WHERE claim_id IN ({placeholders}) "
            "ORDER BY claim_id,review_id",
            claim_ids,
        ).fetchall()
        result: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(str(row["claim_id"]), []).append(
                {
                    "review_id": row["review_id"],
                    "decision": row["decision"],
                    "reason": row["reason"],
                    "transaction_id": row["transaction_id"],
                }
            )
        return result
