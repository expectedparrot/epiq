"""Versioned agent-facing envelopes, actions, schemas, and workflow guidance."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import __version__
from .errors import EpiqError
from .store import LATEST_SCHEMA_VERSION, Store

ENVELOPE_SCHEMA_VERSION = "1.0"
AGENT_SCHEMA_VERSION = "1.0"
SCHEMA_NAMES = {
    "action": "action.schema.json",
    "agent-status": "agent-status.schema.json",
    "envelope": "envelope.schema.json",
}
DOC_NAMES = {
    "agent-interface": "agent-interface.md",
    "workflow": "workflow.md",
}


def action(
    identifier: str,
    label: str,
    argv: list[str],
    *,
    mutates_state: bool,
    uses_network: bool = False,
    requires_approval: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Create an exact, safety-annotated next action."""
    return {
        "id": identifier,
        "label": label,
        "argv": argv,
        "mutates_state": mutates_state,
        "uses_network": uses_network,
        "requires_approval": requires_approval,
        "reason": reason,
    }


def envelope(
    command: str,
    argv: list[str],
    *,
    status: str = "ok",
    data: Any = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    next_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wrap a command result in the stable Epiq transport envelope."""
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "status": status,
        "command": command,
        "argv": argv,
        "data": data,
        "warnings": warnings or [],
        "errors": errors or [],
        "next_steps": next_steps or [],
    }


def version_data() -> dict[str, Any]:
    """Return installed package and public contract versions."""
    return {
        "epiq_version": __version__,
        "package_path": str(files("epiq")),
        "envelope_schema_version": ENVELOPE_SCHEMA_VERSION,
        "agent_schema_version": AGENT_SCHEMA_VERSION,
        "database_schema_version": LATEST_SCHEMA_VERSION,
    }


def capabilities_data() -> dict[str, Any]:
    """Describe the small stable orientation surface for a fresh agent."""
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "interface": "agent-first-json-envelope",
        "default_output": "json-envelope",
        "human_output_opt_in": ["--human", "-H", "--format table"],
        "orientation_commands": {
            "version": ["epiq", "version"],
            "capabilities": ["epiq", "capabilities"],
            "guide": ["epiq", "guide"],
            "status": ["epiq", "agent", "status"],
            "next": ["epiq", "next"],
            "schema": ["epiq", "agent", "schema", "<name>"],
            "doctor": ["epiq", "doctor"],
            "docs": ["epiq", "docs", "list"],
        },
        "schemas": sorted(SCHEMA_NAMES),
        "safety": {
            "next_step_commands_are_argv_arrays": True,
            "mutations_are_declared": True,
            "network_use_is_declared": True,
            "approval_requirements_are_declared": True,
            "errors_use_the_success_envelope": True,
        },
    }


def guide_data() -> dict[str, Any]:
    """Describe the complete Epiq lifecycle and execution boundary."""
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "lifecycle": [
            {
                "stage": "select-project",
                "purpose": "Select or initialize one SQLite research project.",
                "commands": ["epiq db", "epiq use <path>", "epiq init --name <name>"],
            },
            {
                "stage": "design-schema",
                "purpose": "Define entity tables, typed fields, relationships, and formulas.",
                "commands": ["epiq schema", "epiq apply --input <project.json>"],
            },
            {
                "stage": "populate-entities",
                "purpose": "Create stable rows and aliases before researching their fields.",
                "commands": ["epiq entity <kind> <name>", "epiq entity-alias <entity> <alias>"],
            },
            {
                "stage": "research",
                "purpose": "Find gaps, generate bounded tasks, and record sourced findings.",
                "commands": [
                    "epiq gaps --kind <kind>",
                    "epiq refresh-plan --kind <kind>",
                    "epiq record ...",
                ],
            },
            {
                "stage": "review-and-correct",
                "purpose": (
                    "Review proposals, contradictions, schema challenges, and duplicate identities."
                ),
                "commands": [
                    "epiq claim-proposals",
                    "epiq contradictions --kind <kind>",
                    "epiq question-challenges",
                ],
            },
            {
                "stage": "maintain",
                "purpose": "Refresh volatile facts and rematerialize stale derivations.",
                "commands": [
                    "epiq stale --kind <kind>",
                    "epiq stale-derivations",
                    "epiq materialize --kind <kind> --valid-from <date>",
                ],
            },
            {
                "stage": "export-and-protect",
                "purpose": "Export projections and create recoverable project backups.",
                "commands": [
                    "epiq export ...",
                    "epiq backup --output <path>",
                    "epiq export-bundle --output <path>",
                ],
            },
        ],
        "execution_boundary": {
            "epiq": "Defines tasks, validates evidence and values, and owns database writes.",
            "source_provider": "Discovers and captures sources but cannot write claims.",
            "reasoning_backend": "Proposes typed findings but cannot write the database.",
        },
        "resume": "Run `epiq next` after every material stage.",
        "documentation": {name: ["epiq", "docs", "show", name] for name in DOC_NAMES},
    }


def schema_data(name: str) -> dict[str, Any]:
    """Load one bundled JSON Schema by stable name."""
    filename = SCHEMA_NAMES.get(name, name)
    if filename not in SCHEMA_NAMES.values():
        raise EpiqError(
            "schema_not_found",
            f"Unknown agent schema: {name}",
            f"Available schemas: {', '.join(sorted(SCHEMA_NAMES))}",
        )
    resource = files("epiq").joinpath("schemas", filename)
    return {"name": filename, "schema": json.loads(resource.read_text(encoding="utf-8"))}


def docs_list_data() -> dict[str, Any]:
    """List documentation bundled with the installed package."""
    return {
        "documents": [
            {"name": name, "command": ["epiq", "docs", "show", name]} for name in sorted(DOC_NAMES)
        ]
    }


def docs_show_data(name: str) -> dict[str, Any]:
    """Return one bundled Markdown document."""
    filename = DOC_NAMES.get(name)
    if filename is None:
        raise EpiqError(
            "document_not_found",
            f"Unknown documentation topic: {name}",
            f"Available topics: {', '.join(sorted(DOC_NAMES))}",
        )
    markdown = files("epiq").joinpath("docs_content", filename).read_text(encoding="utf-8")
    return {"name": name, "format": "markdown", "markdown": markdown}


def _table_health(store: Store, kind: str) -> dict[str, int | str]:
    projection = store.matrix(kind)
    counts = {"unasked": 0, "not_found": 0, "stale": 0, "contested": 0}
    for row in projection["rows"]:
        for question in projection["questions"]:
            cell = row["cells"][question["name"]]
            state = str(cell["state"])
            if state == "Unasked":
                counts["unasked"] += 1
            elif state == "NotFound":
                counts["not_found"] += 1
            elif state == "Contested":
                counts["contested"] += 1
            if cell.get("temporal", {}).get("freshness") == "stale":
                counts["stale"] += 1
    return {"entity_kind": kind, **counts}


def status_data(store: Store, database: Path, database_source: str) -> dict[str, Any]:
    """Return project state, blockers, queues, and an executable recommendation."""
    overview = store.overview()
    health = [_table_health(store, str(item["kind"])) for item in overview["entity_kinds"]]
    jobs = store.agent_jobs()
    active_jobs = [job for job in jobs if job.get("status") in {"queued", "running"}]
    failed_jobs = [job for job in jobs if job.get("status") == "failed"]
    pending_claims = store.claim_proposals("pending")
    open_challenges = store.question_challenges(status="open")
    integrity = store.doctor()
    blockers = []
    if not integrity["ok"]:
        blockers.append({"code": "integrity_failed", "message": "Project integrity checks failed."})
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "ready": not blockers,
        "database": {"path": str(database.resolve()), "source": database_source},
        "project": overview["project"],
        "tables": overview["entity_kinds"],
        "table_health": health,
        "jobs": {"active": active_jobs, "failed": failed_jobs, "total": len(jobs)},
        "review_queues": {
            "claim_proposals": len(pending_claims),
            "question_challenges": len(open_challenges),
        },
        "integrity": integrity,
        "blockers": blockers,
        "next_actions": next_actions(
            store,
            database,
            database_source,
            precomputed={
                "overview": overview,
                "health": health,
                "active_jobs": active_jobs,
                "failed_jobs": failed_jobs,
                "pending_claims": pending_claims,
                "open_challenges": open_challenges,
                "integrity": integrity,
            },
        ),
    }


def next_actions(
    store: Store,
    database: Path,
    database_source: str,
    *,
    precomputed: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Choose deterministic next actions from current materialized state."""
    if precomputed is None:
        overview = store.overview()
        jobs = store.agent_jobs()
        state = {
            "overview": overview,
            "health": [
                _table_health(store, str(item["kind"])) for item in overview["entity_kinds"]
            ],
            "active_jobs": [
                job for job in jobs if job.get("status") in {"queued", "running"}
            ],
            "failed_jobs": [job for job in jobs if job.get("status") == "failed"],
            "pending_claims": store.claim_proposals("pending"),
            "open_challenges": store.question_challenges(status="open"),
            "integrity": store.doctor(),
        }
    else:
        state = precomputed
    db_prefix = ["epiq", "--db", str(database.resolve())]
    if not state["integrity"]["ok"]:
        return [
            action(
                "inspect-integrity",
                "Inspect project integrity failures",
                [*db_prefix, "doctor"],
                mutates_state=False,
                reason="Database integrity must be restored before further work.",
            )
        ]
    if state["active_jobs"]:
        return [
            action(
                "monitor-research",
                "Monitor active research jobs in the web application",
                ["epiq-web", "--db", str(database.resolve())],
                mutates_state=False,
                uses_network=False,
                reason="Research is already running; avoid duplicate work.",
            )
        ]
    if state["pending_claims"]:
        return [
            action(
                "review-claims",
                "Review pending claim proposals",
                [*db_prefix, "claim-proposals", "--status", "pending"],
                mutates_state=False,
                reason="Provisional claims require a decision before they become current facts.",
            )
        ]
    if state["open_challenges"]:
        return [
            action(
                "review-schema",
                "Review open field-schema challenges",
                [*db_prefix, "question-challenges", "--status", "open"],
                mutates_state=False,
                reason="Field semantics have been challenged and may affect future research.",
            )
        ]
    tables = state["overview"]["entity_kinds"]
    if not tables:
        return [
            action(
                "design-project",
                "Define the first table and fields",
                [*db_prefix, "apply", "--input", "project.json"],
                mutates_state=True,
                reason="The project has no tables.",
            )
        ]
    empty = next((item for item in tables if item["entities"] == 0), None)
    if empty:
        return [
            action(
                "add-row",
                f"Add the first {empty['kind']} row",
                [*db_prefix, "entity", str(empty["kind"]), "<name>"],
                mutates_state=True,
                reason=f"{empty['kind']} has no rows.",
            )
        ]
    fieldless = next((item for item in tables if item["questions"] == 0), None)
    if fieldless:
        return [
            action(
                "add-field",
                f"Define the first {fieldless['kind']} field",
                [
                    *db_prefix,
                    "question",
                    "<field_name>",
                    "--for",
                    str(fieldless["kind"]),
                    "--type",
                    "String",
                ],
                mutates_state=True,
                reason=f"{fieldless['kind']} has no research fields.",
            )
        ]
    contested = next((item for item in state["health"] if item["contested"]), None)
    if contested:
        return [
            action(
                "resolve-contradictions",
                f"Inspect contested {contested['entity_kind']} cells",
                [*db_prefix, "contradictions", "--kind", str(contested["entity_kind"])],
                mutates_state=False,
                reason="Conflicting active claims require curation.",
            )
        ]
    gaps = next((item for item in state["health"] if item["unasked"] or item["not_found"]), None)
    if gaps:
        return [
            action(
                "plan-research",
                f"Plan research for {gaps['entity_kind']} gaps",
                [
                    *db_prefix,
                    "refresh-plan",
                    "--kind",
                    str(gaps["entity_kind"]),
                    "--include",
                    "gaps",
                ],
                mutates_state=False,
                reason="The table contains unanswered or unsuccessful cells.",
            )
        ]
    stale = next((item for item in state["health"] if item["stale"]), None)
    if stale:
        return [
            action(
                "refresh-stale",
                f"Plan refreshes for stale {stale['entity_kind']} cells",
                [
                    *db_prefix,
                    "refresh-plan",
                    "--kind",
                    str(stale["entity_kind"]),
                    "--include",
                    "stale",
                ],
                mutates_state=False,
                reason="Temporal policy marks one or more cells stale.",
            )
        ]
    return [
        action(
            "verify-project",
            "Verify project integrity",
            [*db_prefix, "doctor"],
            mutates_state=False,
            reason=f"Project is current; database selection source is {database_source}.",
        )
    ]
