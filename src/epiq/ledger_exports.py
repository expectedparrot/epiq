"""Deterministic tabular and native EDSL exports from ledger projections."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from .edsl_export import write_edsl
from .errors import EpiqError
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
    if export_format == "matrix-json":
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
