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

    def entities(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT e.entity_id,e.kind,e.name,e.attributes_json,e.role,e.identity_json,
                   NOT EXISTS (
                       SELECT 1 FROM projected_entity_visibility_events visibility
                       WHERE visibility.entity_id=e.entity_id AND visibility.visible=0
                       AND NOT EXISTS (
                           SELECT 1 FROM projected_entity_visibility_events later
                           JOIN ledger_transactions lt ON lt.transaction_id=later.transaction_id
                           JOIN ledger_transactions current
                             ON current.transaction_id=visibility.transaction_id
                           WHERE later.entity_id=e.entity_id AND lt.ordinal>current.ordinal
                       )
                   ) AS visible
            FROM projected_entities e
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
                "visible": bool(row["visible"]),
            }
            for row in rows
        ]

    def questions(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT q.question_id,q.name,q.subject_kind,q.value_type,q.definition_json
            FROM projected_questions q
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
            }
            for row in rows
        ]

    def matrix(self, kind: str) -> dict[str, Any]:
        entities = [item for item in self.entities(kind=kind) if item["visible"]]
        questions = self.questions(kind=kind)
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
