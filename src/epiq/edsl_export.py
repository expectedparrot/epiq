"""Export Epiq projections as native EDSL git-backed packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .errors import EpiqError


def projection_records(matrix: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Map current Epiq rows to EDSL-friendly records and a descriptive codebook."""
    questions = matrix["questions"]
    codebook = {
        "epiq_entity_id": "Stable Epiq entity identifier",
        "entity_name": f"{matrix['entity_kind']} row name",
        **{
            str(question["name"]): str(
                question["definition"].get("label", question["name"])
            )
            for question in questions
        },
    }
    records = []
    for row in matrix["rows"]:
        record: dict[str, Any] = {
            "epiq_entity_id": str(row["entity_id"]),
            "entity_name": str(row["name"]),
        }
        for question in questions:
            cell = row["cells"][question["name"]]
            if cell.get("references"):
                names = [item["name"] for item in cell["references"]]
                record[question["name"]] = (
                    names
                    if question["definition"].get("cardinality", "one") == "many"
                    else names[0]
                )
            elif cell["state"] == "Answered":
                record[question["name"]] = cell.get("value", cell.get("values"))
            elif cell["state"] == "Contested":
                record[question["name"]] = cell.get("values", [])
            else:
                record[question["name"]] = None
        records.append(record)
    return records, codebook


def write_edsl(
    matrix: dict[str, Any],
    output: str | Path,
    object_type: Literal["scenario-list", "agent-list"],
) -> Path:
    """Write a loadable EDSL ScenarioList or AgentList `.ep` package."""
    try:
        from edsl import Agent, AgentList, Scenario, ScenarioList
    except ImportError as error:
        raise EpiqError(
            "edsl_not_installed",
            "EDSL is required for .ep exports",
            "Install Epiq with the web or edsl extra, then retry.",
        ) from error
    records, codebook = projection_records(matrix)
    if object_type == "scenario-list":
        exported = ScenarioList(
            [Scenario(record, name=record["entity_name"]) for record in records],
            codebook=codebook,
        )
    elif object_type == "agent-list":
        exported = AgentList(
            [
                Agent(
                    name=record["entity_name"],
                    traits={key: value for key, value in record.items() if key != "entity_name"},
                    codebook={
                        key: value for key, value in codebook.items() if key != "entity_name"
                    },
                )
                for record in records
            ],
            codebook={key: value for key, value in codebook.items() if key != "entity_name"},
        )
    else:
        raise EpiqError("invalid_edsl_type", f"Unknown EDSL export type: {object_type}")
    output_path = Path(output).resolve()
    if output_path.suffix != ".ep":
        output_path = output_path.with_suffix(".ep")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        if output_path.stat().st_size == 0:
            output_path.unlink()
        else:
            raise EpiqError(
                "export_exists",
                f"Export already exists: {output_path}",
                "Choose another --output-path or remove the existing export explicitly.",
            )
    exported.git.save(output_path)
    return output_path
