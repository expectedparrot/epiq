"""Versioned agent-facing envelopes, actions, schemas, and workflow guidance."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from importlib.resources import files
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from . import __version__
from .errors import EpiqError
from .schema_guidance import SCHEMA_DESIGN_CHECKLIST, pattern_summaries
from .store import LATEST_SCHEMA_VERSION, Store

ENVELOPE_SCHEMA_VERSION = "1.0"
AGENT_SCHEMA_VERSION = "1.0"
SCHEMA_NAMES = {
    "batch-write": "batch-write-1.0.schema.json",
    "source-request": "source-request-1.schema.json",
    "source-bundle": "source-bundle-1.schema.json",
    "research-finding": "research-finding-1.schema.json",
    "action": "action.schema.json",
    "agent-status": "agent-status.schema.json",
    "envelope": "envelope.schema.json",
    "ledger-operation": "ledger-operation-1.schema.json",
    "ledger-transaction": "ledger-transaction-1.schema.json",
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


def install_status_data() -> dict[str, Any]:
    """Report local installation and optional integration readiness without exposing secrets."""
    package_root = files("epiq")
    optional_modules = {
        name: find_spec(name) is not None for name in ("fastapi", "uvicorn", "edsl")
    }
    source_provider = os.environ.get("EPIQ_SOURCE_PROVIDER") or "openai"
    return {
        **version_data(),
        "python": {
            "version": platform.python_version(),
            "supported": sys.version_info >= (3, 11),
            "executable": sys.executable,
        },
        "commands": {
            "epiq": shutil.which("epiq"),
            "epiq-web": shutil.which("epiq-web"),
            "pi": shutil.which(os.environ.get("EPIQ_PI_EXECUTABLE", "pi")),
            "git": shutil.which("git"),
        },
        "optional_modules": optional_modules,
        "web": {
            "dependencies_ready": optional_modules["fastapi"] and optional_modules["uvicorn"],
            "frontend_bundled": package_root.joinpath("_frontend", "index.html").is_file(),
        },
        "research": {
            "source_provider": source_provider,
            "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
            "firecrawl_key_configured": bool(os.environ.get("FIRECRAWL_API_KEY")),
            "pi_available": bool(
                shutil.which(os.environ.get("EPIQ_PI_EXECUTABLE", "pi"))
            ),
        },
        "recommended_actions": {
            "install": [
                "uv",
                "tool",
                "install",
                "git+https://github.com/expectedparrot/epiq.git",
            ],
            "upgrade": ["uv", "tool", "upgrade", "epiq"],
            "verify": ["epiq", "install-status"],
        },
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
            "install_status": ["epiq", "install-status"],
            "capabilities": ["epiq", "capabilities"],
            "guide": ["epiq", "guide"],
            "guide_topics": ["epiq", "guide", "<topic>"],
            "project_status": ["epiq", "--project", "<directory>", "status"],
            "schema": ["epiq", "agent", "schema", "<name>"],
            "doctor": ["epiq", "--project", "<directory>", "doctor"],
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


GUIDE_TOPICS: dict[str, dict[str, Any]] = {
    "quickstart": {
        "purpose": "Create or open a Git-ledger project and record one sourced fact.",
        "commands": [
            ["epiq", "--project", "research", "init", "--name", "Research"],
            ["epiq", "--project", "research", "kind", "Company"],
            ["epiq", "--project", "research", "entity", "Company", "Acme"],
            [
                "epiq",
                "--project",
                "research",
                "question",
                "employees",
                "--for",
                "Company",
                "--type",
                "Int",
            ],
            ["epiq", "--project", "research", "record", "..."],
            ["epiq", "--project", "research", "matrix", "--kind", "Company"],
            ["epiq", "--project", "research", "doctor"],
        ],
        "notes": [
            "Install with `uv tool install git+https://github.com/expectedparrot/epiq.git`.",
            "Before work, run `uv tool upgrade epiq` and `epiq version` to avoid stale commands.",
            "Use IDs returned by entity and question commands; do not invent them.",
            "Use --actor and --idempotency-key on automated writes.",
            "Run `epiq guide evidence` for the complete record contract.",
        ],
    },
    "workflow": {
        "purpose": "Follow the normal ledger-native research lifecycle.",
        "stages": [
            {"stage": "orient", "commands": [["epiq", "guide"], ["epiq", "status"]]},
            {
                "stage": "model",
                "commands": [["epiq", "kind", "<kind>"], ["epiq", "question", "..."]],
            },
            {"stage": "populate", "commands": [["epiq", "entity", "<kind>", "<name>"]]},
            {
                "stage": "research",
                "commands": [["epiq", "record", "..."], ["epiq", "not-found", "..."]],
            },
            {"stage": "review", "commands": [["epiq", "conflicts"], ["epiq", "proposals"]]},
            {"stage": "verify", "commands": [["epiq", "doctor"], ["epiq", "build", "--verify"]]},
            {"stage": "share", "commands": [["epiq", "remote", "publish", "--message", "..."]]},
        ],
    },
    "schema-design": {
        "purpose": "Design a collection schema before creating kinds and questions.",
        "checklist": SCHEMA_DESIGN_CHECKLIST,
        "agent_behavior": [
            "Do not merely translate nouns in the request into one flat table.",
            (
                "Inventory the surrounding domain: people, organizations, outputs, events, topics, "
                "and media."
            ),
            (
                "Present a minimal core, recommended extensions, and optional enrichments with "
                "tradeoffs."
            ),
            "Explain which future questions each proposed entity or relationship makes answerable.",
            "Ask the user to review scope before emitting or executing creation commands.",
        ],
        "principles": [
            "Entities have stable identity and independent evidence-backed history.",
            "Relationships use Ref[Kind], not embedded names or comma-separated strings.",
            "Repeated events become observation entities instead of overwritten fields.",
            (
                "Propose a minimal core and natural extensions for outputs, events, and public "
                "context."
            ),
            "Every question declares a type, cardinality, temporal policy, and evidence rule.",
            "Pilot typical and adversarial cases before scaling collection.",
        ],
        "next": ["epiq", "guide", "schema-patterns"],
    },
    "schema-patterns": {
        "purpose": "Start from a reviewable collection pattern and adapt it to the project.",
        "patterns": pattern_summaries(),
        "commands": [
            ["epiq", "schema-plan", "faculty"],
            ["epiq", "schema-plan", "literature"],
            ["epiq", "schema-plan", "startups"],
        ],
        "rule": "A schema plan is guidance only and never mutates a project.",
    },
    "evidence": {
        "purpose": "Record claims with bounded source evidence and explicit time semantics.",
        "command": [
            "epiq", "record", "--subject-id", "<entity-id>", "--question-id", "<question-id>",
            "--value", "<json-value>", "--valid-from", "<date>", "--source-title", "<title>",
            "--source-type", "<type>", "--url", "<url>", "--retrieved-at", "<date>",
            "--excerpt", "<bounded-passage>", "--idempotency-key", "<stable-key>",
        ],
        "rules": [
            "Never invent evidence or use not-found as a negative factual answer.",
            "retrieved-at is when evidence was obtained; valid-from is when the fact became true.",
            "Use propose/review when a human or another agent must approve the conclusion.",
        ],
    },
    "git": {
        "purpose": "Collaborate through Git while Epiq transaction IDs remain domain identities.",
        "commands": [
            ["epiq", "remote", "clone", "<url>", "--path", "<directory>"],
            ["epiq", "remote", "fetch"],
            ["git", "merge", "<remote-ref>"],
            ["epiq", "merge-check"],
            ["epiq", "conflicts"],
            ["epiq", "remote", "publish", "--message", "<message>", "--pr"],
        ],
        "rules": [
            "Fetch never silently integrates remote changes.",
            "After merge or rebase, run merge-check before publishing.",
            "Do not commit .epiq; it is a disposable local projection.",
        ],
    },
    "recovery": {
        "purpose": "Diagnose and recover without editing transaction files or SQLite directly.",
        "commands": [
            ["epiq", "validate"],
            ["epiq", "build", "--force", "--verify"],
            ["epiq", "doctor"],
            ["epiq", "conflicts"],
        ],
        "rules": [
            "Deleting .epiq/projection.sqlite is safe; deleting ledger files is not.",
            "Resolve Git text conflicts explicitly, then let Epiq report domain conflicts.",
            "Use corrective operations; never rewrite an acknowledged transaction.",
        ],
    },
    "exports": {
        "purpose": "Create deterministic projections without changing authoritative data.",
        "commands": [
            ["epiq", "export", "--format", "matrix-csv", "--kind", "<kind>", "--output", "<file>"],
            ["epiq", "export", "--format", "claims-jsonl", "--output", "<file>"],
            [
                "epiq",
                "export-html",
                "--kind",
                "<kind>",
                "--output",
                "<file.html>",
            ],
            [
                "epiq",
                "export",
                "--format",
                "scenario-list",
                "--kind",
                "<kind>",
                "--output",
                "<file.ep>",
            ],
        ],
    },
}


def guide_data(topic: str = "quickstart") -> dict[str, Any]:
    """Return ledger-native, machine-readable guidance for one workflow topic."""
    if topic not in GUIDE_TOPICS:
        raise EpiqError(
            "guide_topic_not_found",
            f"Unknown guide topic: {topic}",
            f"Available topics: {', '.join(sorted(GUIDE_TOPICS))}",
        )
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "architecture": {
            "authority": "Git-versioned immutable transaction files",
            "projection": ".epiq/projection.sqlite is disposable and must not be committed",
            "selection": ["--project <directory>", "EPIQ_PROJECT", "epiq use-project <directory>"],
        },
        "installation": {
            "install": [
                "uv",
                "tool",
                "install",
                "git+https://github.com/expectedparrot/epiq.git",
            ],
            "upgrade": ["uv", "tool", "upgrade", "epiq"],
            "verify": ["epiq", "version"],
            "source_checkout": ["git", "pull", "--ff-only"],
            "source_sync": ["uv", "sync", "--extra", "test"],
        },
        "topic": topic,
        "available_topics": sorted(GUIDE_TOPICS),
        "guidance": GUIDE_TOPICS[topic],
        "more": ["epiq", "guide", "<topic>"],
        "schemas": [
            ["epiq", "agent", "schema", "ledger-transaction"],
            ["epiq", "agent", "schema", "ledger-operation"],
            ["epiq", "agent", "schema", "envelope"],
        ],
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
    if state["integrity"].get("provenance", {}).get("findings"):
        return [action(
            "inspect-provenance", "Inspect capture, claim support, and review gaps",
            [*db_prefix, "doctor"], mutates_state=False,
            reason="Populated cells still require source or review checks.",
        )]
    return [
        action(
            "verify-project",
            "Verify project integrity",
            [*db_prefix, "doctor"],
            mutates_state=False,
            reason=f"Project is current; database selection source is {database_source}.",
        )
    ]
