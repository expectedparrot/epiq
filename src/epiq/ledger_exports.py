"""Deterministic tabular and native EDSL exports from ledger projections."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .edsl_export import write_edsl
from .errors import EpiqError
from .html import HTML
from .ledger import canonical_bytes
from .ledger_api import LedgerDatabase


def export_ledger(
    database: LedgerDatabase,
    output: str | Path,
    export_format: str,
    *,
    kind: str | None = None,
    include_closed: bool = False,
) -> Path:
    """Write one deterministic ledger-backed export, or a native EDSL package."""
    destination = Path(output).resolve()
    if destination.exists():
        raise EpiqError("export_exists", f"Export already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if export_format in {"scenario-list", "agent-list"}:
        if kind is None:
            raise EpiqError("export_kind_required", "EDSL exports require --kind")
        return write_edsl(
            legacy_matrix(database, kind),
            destination,
            export_format,
        )
    if export_format == "html":
        content = _html(database, kind).encode()
    elif export_format == "matrix-json":
        _require_kind(kind)
        content = canonical_bytes(database.matrix(kind)) + b"\n"
    elif export_format == "matrix-csv":
        _require_kind(kind)
        content = _matrix_csv(database, kind).encode()
    elif export_format == "claims-jsonl":
        claims = database.claims(kind=kind, include_closed=include_closed)
        content = b"".join(canonical_bytes(item) + b"\n" for item in claims)
    elif export_format == "claims-csv":
        content = _claims_csv(
            database.claims(kind=kind, include_closed=include_closed)
        ).encode()
    else:
        raise EpiqError("invalid_export_format", f"Unknown ledger export format: {export_format}")
    destination.write_bytes(content)
    return destination


def _html(database: LedgerDatabase, kind: str | None) -> str:
    transactions = database.repository.ordered()
    kinds = database.kinds()
    if kind is not None and kind not in kinds:
        raise EpiqError(
            "entity_kind_not_found",
            f"Unknown entity kind {kind!r}; choose from: {', '.join(kinds) or 'none'}",
        )
    selected = [kind] if kind else kinds
    evidence = {item["evidence_id"]: item for item in database.evidence()}
    outcomes = database.research_outcomes(kind=kind)
    outcome_cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for outcome in outcomes:
        outcome_cells.setdefault(
            (str(outcome["subject_id"]), str(outcome["question_id"])), []
        ).append(outcome)
    tables = [
        _html_table(database, selected_kind, evidence, outcome_cells)
        for selected_kind in selected
    ]
    events = []
    sequence = 0
    for transaction in transactions:
        for operation in transaction.operations:
            sequence += 1
            events.append(
                {
                    "seq": sequence,
                    "event_type": operation.type,
                    "actor": transaction.actor,
                    "recorded_at": transaction.recorded_at,
                    "transaction_id": transaction.transaction_id,
                    "operation_id": operation.operation_id,
                    "payload": operation.payload,
                }
            )
    conflicts = database.conflicts()
    data = {
        "snapshot_version": "ledger-1.0",
        "generated_at": transactions[-1].recorded_at if transactions else None,
        "overview": {
            "project": database.project(),
            "entity_kinds": [
                {
                    "kind": selected_kind,
                    "entities": len(database.entities(kind=selected_kind)),
                    "questions": len(database.questions(kind=selected_kind)),
                }
                for selected_kind in selected
            ],
        },
        "tables": tables,
        "events": events,
        "review": {
            "claim_proposals": database.proposals(),
            "question_challenges": database.question_challenges(status=None),
            "agent_jobs": [],
        },
        "integrity": {
            "ok": True,
            "findings": conflicts,
            "transaction_set_digest": database.repository.transaction_set_digest(transactions),
        },
    }
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return HTML.replace("__DATA__", encoded)


def _html_table(
    database: LedgerDatabase,
    kind: str,
    evidence: dict[str, dict[str, Any]],
    outcome_cells: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    projection = database.matrix(kind)
    table = legacy_matrix(database, kind)
    rows = {item["entity_id"]: item for item in table["rows"]}
    questions = {item["question_id"]: item for item in projection["questions"]}
    for entity_id, cells in projection["cells"].items():
        for question_id, claims in cells.items():
            question = questions[question_id]
            cell = rows[entity_id]["cells"][question["name"]]
            cell["lineage"] = [
                _html_lineage(claim, evidence_item)
                for claim in claims
                for evidence_id in claim["evidence_ids"] or [None]
                for evidence_item in ([evidence.get(evidence_id)] if evidence_id else [None])
            ]
            outcomes = outcome_cells.get((entity_id, question_id), [])
            if not claims and outcomes:
                cell["state"] = "NotFound"
                cell["research"] = outcomes
    return table


def _html_lineage(
    claim: dict[str, Any], evidence: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        **claim,
        "evidence_id": evidence["evidence_id"] if evidence else None,
        "excerpt": evidence["excerpt"] if evidence else None,
        "source": (
            {"title": evidence["title"], "url": evidence["url"]} if evidence else None
        ),
        "provenance": evidence.get("capture", {}) if evidence else {},
        "support": claim["evidence_ids"],
        "review": {"status": "reviewed" if claim["reviews"] else "unreviewed"},
    }


def legacy_matrix(database: LedgerDatabase, kind: str) -> dict[str, Any]:
    """Adapt the lossless ledger matrix to EDSL's row-oriented projection contract."""
    projection = database.matrix(kind)
    names = {item["entity_id"]: item["name"] for item in database.entities()}
    rows = []
    for entity in projection["entities"]:
        cells = {}
        for question in projection["questions"]:
            claims = projection["cells"][entity["entity_id"]][question["question_id"]]
            values = [claim["value"] for claim in claims]
            state = "Unasked" if not values else "Answered" if len(values) == 1 else "Contested"
            cell: dict[str, Any] = {
                "state": state,
                "value": values[0] if len(values) == 1 else None,
                "values": values,
            }
            if str(question["value_type"]).startswith("Ref[") and values:
                identifiers = values[0] if isinstance(values[0], list) else values
                cell["references"] = [
                    {"entity_id": identifier, "name": names.get(identifier, identifier)}
                    for identifier in identifiers
                ]
            cells[question["name"]] = cell
        rows.append({**entity, "cells": cells})
    return {"entity_kind": kind, "questions": projection["questions"], "rows": rows}


def _matrix_csv(database: LedgerDatabase, kind: str) -> str:
    matrix = legacy_matrix(database, kind)
    questions = matrix["questions"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=["epiq_entity_id", "entity_name", *(item["name"] for item in questions)],
        lineterminator="\n",
    )
    writer.writeheader()
    for row in matrix["rows"]:
        record: dict[str, Any] = {
            "epiq_entity_id": row["entity_id"],
            "entity_name": row["name"],
        }
        for question in questions:
            cell = row["cells"][question["name"]]
            value = cell["value"] if cell["state"] == "Answered" else cell["values"] or None
            record[question["name"]] = _csv_value(value)
        writer.writerow(record)
    return output.getvalue()


def _claims_csv(claims: list[dict[str, Any]]) -> str:
    fields = [
        "claim_id",
        "subject_id",
        "subject_name",
        "kind",
        "question_id",
        "question_name",
        "value",
        "valid_from",
        "confidence",
        "temporal_basis",
        "transaction_id",
        "actor",
        "recorded_at",
        "evidence_ids",
        "reviews",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for claim in claims:
        writer.writerow(
            {
                key: _csv_value(claim[key]) if isinstance(claim[key], dict | list) else claim[key]
                for key in fields
            }
        )
    return output.getvalue()


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return canonical_bytes(value).decode()
    return value


def _require_kind(kind: str | None) -> None:
    if kind is None:
        raise EpiqError("export_kind_required", "Matrix exports require --kind")
