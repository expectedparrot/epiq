"""Bulk, snapshot-consistent projections of the epistemic ledger."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any

from .errors import EpiqError


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Projection:
    """Load a table and its lineage in batches, respecting transaction time everywhere."""

    def __init__(self, connection: sqlite3.Connection, cutoff: str) -> None:
        self.connection = connection
        self.cutoff = cutoff
        self.evidence: dict[str, list[sqlite3.Row]] = defaultdict(list)
        self.derivations: dict[str, sqlite3.Row] = {}
        self.inputs: dict[str, list[str]] = defaultdict(list)
        self.dependencies: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.assessments: dict[str, sqlite3.Row] = {}

    def batch(self, sql: str, ids: list[str], temporal: bool = True) -> list[sqlite3.Row]:
        rows = []
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                self.connection.execute(
                    sql.replace("__IDS__", placeholders),
                    (*chunk, self.cutoff) if temporal else chunk,
                ).fetchall()
            )
        return rows

    def load_lineage(self, claims: list[sqlite3.Row]) -> None:
        ids = [str(claim["claim_id"]) for claim in claims]
        for row in self.batch(
            """SELECT ce.claim_id,ce.evidence_id,e.excerpt,s.url,s.source_type,s.title,
                      s.published_at,s.retrieved_at,source_event.actor evidence_actor,
                      source_event.payload_json evidence_payload,s.locator_json,s.linked_entity_id
               FROM claim_evidence ce
               JOIN evidence e ON e.evidence_id=ce.evidence_id
               JOIN sources s ON s.source_id=e.source_id
               JOIN events source_event ON source_event.seq=e.created_seq
               JOIN events ev ON ev.seq=ce.created_seq
               JOIN claims owner ON owner.claim_id=ce.claim_id
               WHERE ce.claim_id IN (__IDS__)
               AND (ce.created_seq=owner.created_seq OR ev.recorded_at<=?)
               ORDER BY ce.ordinal""",
            ids,
        ):
            self.evidence[str(row["claim_id"])].append(row)
        for row in self.batch(
            "SELECT * FROM derivations WHERE claim_id IN (__IDS__)",
            ids,
            False,
        ):
            self.derivations[str(row["claim_id"])] = row
        for row in self.batch(
            """SELECT ci.claim_id,ci.input_claim_id FROM claim_inputs ci
               JOIN events ev ON ev.seq=ci.created_seq
               JOIN claims owner ON owner.claim_id=ci.claim_id
               WHERE ci.claim_id IN (__IDS__)
               AND (ci.created_seq=owner.created_seq OR ev.recorded_at<=?)
               ORDER BY ci.ordinal""",
            ids,
        ):
            self.inputs[str(row["claim_id"])].append(str(row["input_claim_id"]))
        for row in self.batch(
            """SELECT d.claim_id,d.dependency_claim_id,d.role FROM derivation_dependencies d
               JOIN events ev ON ev.seq=d.created_seq
               JOIN claims owner ON owner.claim_id=d.claim_id
               WHERE d.claim_id IN (__IDS__)
               AND (d.created_seq=owner.created_seq OR ev.recorded_at<=?)
               ORDER BY d.role,d.ordinal""",
            ids,
        ):
            self.dependencies[str(row["claim_id"])].append(
                {
                    "claim_id": str(row["dependency_claim_id"]),
                    "role": str(row["role"]),
                }
            )
        evidence_ids = list(
            {str(row["evidence_id"]) for rows in self.evidence.values() for row in rows}
        )
        for row in self.batch(
            """SELECT a.* FROM evidence_assessments a JOIN events ev ON ev.seq=a.created_seq
               WHERE a.evidence_id IN (__IDS__) AND ev.recorded_at<=?
               ORDER BY a.created_seq DESC""",
            evidence_ids,
        ):
            self.assessments.setdefault(str(row["evidence_id"]), row)

    def matrix(
        self,
        entity_kind: str,
        question_names: list[str] | None,
        known_at: str | None,
        valid_at: str | None,
        actor: str | None,
    ) -> dict[str, Any]:
        connection, cutoff = self.connection, self.cutoff
        questions_by_name = {}
        question_ids = {}
        for question in connection.execute(
            """SELECT q.* FROM questions q JOIN events ev ON ev.seq=q.created_seq
               WHERE q.subject_kind=? AND ev.recorded_at<=? ORDER BY q.created_seq""",
            (entity_kind, cutoff),
        ):
            questions_by_name[str(question["name"])] = question
            question_ids[str(question["question_id"])] = str(question["name"])
        # Visibility tables hold only the latest state; replay the bounded event subset.
        entity_visible, question_visible = {}, {}
        for event in connection.execute(
            """SELECT event_type,payload_json FROM events WHERE event_type IN
               ('entity.retire','entity.restore','question.retire','question.restore',
                'question.evolve') AND recorded_at<=? ORDER BY seq""",
            (cutoff,),
        ):
            payload = json.loads(event["payload_json"])
            if event["event_type"].startswith("entity."):
                entity_visible[payload["entity_id"]] = event["event_type"] == "entity.restore"
            elif event["event_type"] == "question.evolve":
                name = question_ids.get(payload["predecessor_question_id"])
                successors = {question_ids.get(qid) for qid in payload["successor_question_ids"]}
                if name and payload["retired_predecessor"] and name not in successors:
                    question_visible[name] = False
            else:
                question_visible[payload["name"]] = event["event_type"] == "question.restore"
        questions = [
            q
            for q in questions_by_name.values()
            if question_visible.get(q["name"], True)
            and (not question_names or q["name"] in question_names)
        ]
        names = {str(q["name"]) for q in questions}
        # Include reference kinds to resolve display labels without a query per cell.
        kinds = {entity_kind}
        kinds.update(q["value_type"][4:-1] for q in questions if q["value_type"].startswith("Ref["))
        placeholders = ",".join("?" for _ in kinds)
        entities = {
            str(row["entity_id"]): row
            for row in connection.execute(
                f"""SELECT e.* FROM entities e JOIN events ev ON ev.seq=e.created_seq
                WHERE e.kind IN ({placeholders}) AND ev.recorded_at<=? ORDER BY e.name""",
                (*kinds, cutoff),
            )
        }
        redirects = {
            str(row["from_entity_id"]): str(row["into_entity_id"])
            for row in connection.execute(
                f"""SELECT r.* FROM entity_redirects r JOIN events ev ON ev.seq=r.created_seq
                JOIN entities e ON e.entity_id=r.from_entity_id
                WHERE e.kind IN ({placeholders}) AND ev.recorded_at<=?""",
                (*kinds, cutoff),
            )
        }

        def survivor(entity_id: str) -> str:
            seen = set()
            while entity_id in redirects:
                if entity_id in seen:
                    raise EpiqError(
                        "entity_merge_cycle", f"Entity merge cycle includes {entity_id}"
                    )
                seen.add(entity_id)
                entity_id = redirects[entity_id]
            return entity_id

        survivors = {entity_id: survivor(entity_id) for entity_id in entities}
        visible = {
            entity_id: row
            for entity_id, row in entities.items()
            if row["kind"] == entity_kind
            and entity_id not in redirects
            and entity_visible.get(entity_id, True)
        }
        clusters: dict[str, list[str]] = defaultdict(list)
        for entity_id, target in survivors.items():
            if target in visible and entity_id != target:
                clusters[target].append(entity_id)
        aliases: dict[str, list[str]] = defaultdict(list)
        for row in connection.execute(
            """SELECT a.* FROM entity_aliases a JOIN events ev ON ev.seq=a.created_seq
               JOIN entities e ON e.entity_id=a.entity_id
               WHERE e.kind=? AND ev.recorded_at<=? ORDER BY a.created_seq""",
            (entity_kind, cutoff),
        ):
            aliases[survivors[str(row["entity_id"])]].append(str(row["alias"]))
        valid = valid_at or "9999-12-31"
        claims_by_cell: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        claims = []
        for claim in connection.execute(
            """SELECT c.*,q.name question_name,ev.actor claim_actor,ev.payload_json claim_payload
               FROM claims c JOIN questions q ON q.question_id=c.question_id
               JOIN events ev ON ev.seq=c.created_seq
               WHERE q.subject_kind=? AND (? IS NULL OR ev.actor=?)
               AND c.tx_from<=? AND (c.tx_to IS NULL OR c.tx_to>?)
               AND c.valid_from<=? AND (c.valid_to IS NULL OR c.valid_to>?)
               AND NOT EXISTS (
                 SELECT 1 FROM claim_validity_ends ve JOIN events vee ON vee.seq=ve.created_seq
                 WHERE ve.claim_id=c.claim_id AND vee.recorded_at<=? AND ve.valid_to<=?
               ) ORDER BY c.valid_from DESC,c.created_seq DESC""",
            (entity_kind, actor, actor, cutoff, cutoff, valid, valid, cutoff, valid),
        ):
            target = survivors.get(str(claim["subject_id"]))
            if target in visible and claim["question_name"] in names:
                claims.append(claim)
                claims_by_cell[target, str(claim["question_name"])].append(claim)
        tasks = {}
        for row in connection.execute(
            """SELECT t.*,q.name question_name FROM research_tasks t
               JOIN questions q ON q.question_id=t.question_id
               JOIN events ev ON ev.seq=t.created_seq
               WHERE q.subject_kind=? AND ev.recorded_at<=? ORDER BY t.created_seq DESC""",
            (entity_kind, cutoff),
        ):
            target = survivors.get(str(row["subject_id"]))
            tasks.setdefault((target, str(row["question_name"])), row)
        challenges: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in connection.execute(
            """SELECT qc.*,q.name FROM question_challenges qc
               JOIN questions q ON q.question_id=qc.question_id
               JOIN events ev ON ev.seq=qc.created_seq
               LEFT JOIN events closed ON closed.seq=qc.closed_seq
               WHERE q.subject_kind=? AND ev.recorded_at<=?
               AND (qc.closed_seq IS NULL OR closed.recorded_at>?) ORDER BY qc.created_seq""",
            (entity_kind, cutoff, cutoff),
        ):
            challenges[str(row["name"])].append(
                {key: str(row[key]) for key in ("challenge_id", "problem", "explanation")}
            )
        self.load_lineage(claims)
        rows = []
        reference_date = date.fromisoformat(known_at[:10]) if known_at else date.today()
        definitions = {str(q["name"]): json.loads(q["definition_json"]) for q in questions}
        for entity_id, entity in visible.items():
            cells = {}
            for question in questions:
                name = str(question["name"])
                cell = self.project_cell(
                    claims_by_cell[entity_id, name],
                    definitions[name],
                    tasks.get((entity_id, name)),
                    reference_date,
                )
                if question["value_type"].startswith("Ref["):
                    references = []
                    for value in cell.get("values", []):
                        if not isinstance(value, str):
                            continue
                        target = entities.get(survivors.get(value, value))
                        if target is None:
                            continue
                        item = {key: str(target[key]) for key in ("entity_id", "name", "kind")}
                        if item not in references:
                            references.append(item)
                    cell["references"] = references
                    if cell.get("value") is not None and references:
                        cell["display_value"] = references[0]
                    cell["display_values"] = references
                cells[name] = cell
            rows.append(
                {
                    "entity_id": entity_id,
                    "name": entity["name"],
                    "aliases": aliases[entity_id],
                    "merged_entity_ids": clusters[entity_id],
                    "attributes": json.loads(entity["attributes_json"]),
                    "cells": cells,
                }
            )
        return {
            "entity_kind": entity_kind,
            "known_at": known_at,
            "valid_at": valid_at,
            "actor": actor,
            "questions": [
                {
                    "question_id": q["question_id"],
                    "name": q["name"],
                    "value_type": q["value_type"],
                    "definition": definitions[q["name"]],
                    "schema_state": "challenged" if challenges[q["name"]] else "active",
                    "open_challenges": challenges[q["name"]],
                }
                for q in questions
            ],
            "rows": rows,
        }

    def project_cell(
        self,
        claims: list[sqlite3.Row],
        definition: dict[str, Any],
        task: sqlite3.Row | None = None,
        reference_date: date | None = None,
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
            claim_id = str(claim["claim_id"])
            evidence_rows = self.evidence[claim_id]
            derivation = self.derivations.get(claim_id)
            claim_payload = json.loads(str(claim["claim_payload"]))
            input_claim_ids = self.inputs[claim_id]
            dependencies = self.dependencies[claim_id]
            for evidence_row in evidence_rows:
                assessment = self.assessments.get(str(evidence_row["evidence_id"]))
                item = {
                    "token": f"p_{claim['claim_id']}",
                    "claim_id": claim["claim_id"],
                    "value": json.loads(claim["value_json"]),
                    "confidence": claim["confidence"],
                    "actor": str(claim["claim_actor"]),
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
                age_days = (
                    (reference_date or date.today()) - date.fromisoformat(newest_as_of[:10])
                ).days
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
