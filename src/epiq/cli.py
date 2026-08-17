"""JSON-first command-line interface for Epiq."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .demo import load_patriots
from .dsl import describe, parse
from .errors import EpiqError
from .html import write_html
from .importers import import_cham_corpus
from .store import QUESTION_CHALLENGE_PROBLEMS, Store
from .xlsx import write_xlsx

CONFIG_PATH = Path(".epiq/config.json")
DEFAULT_DB = Path(".epiq/epiq.sqlite")


def _emit(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _attributes(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise EpiqError("invalid_attributes", "Attributes must be a JSON object")
    return result


def _entity_name(kind: str, name: str | None, identity: dict[str, Any] | None) -> str:
    if name:
        return name
    if not identity:
        raise EpiqError("entity_name_required", "Entity requires a name or --identity")
    parts = ", ".join(f"{key}={_display(value)}" for key, value in sorted(identity.items()))
    return f"{kind}[{parts}]"


def _json_file(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise EpiqError("invalid_json", f"Invalid JSON input: {error}") from error


def _predicate(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.fullmatch(
        r"\s*([a-z_][a-z0-9_]*)\s*(<=|>=|!=|=|<|>|contains_all|contains_any|contains|any_ref|state)\s*(.+?)\s*",
        raw,
    )
    if not match:
        raise EpiqError(
            "invalid_query_predicate",
            f"Cannot parse --where {raw!r}",
            "Use field=value, 'field >= 10', or a JSON predicate object.",
        )
    question, operator, value = match.groups()
    operations = {
        "=": "eq",
        "!=": "ne",
        ">": "gt",
        ">=": "gte",
        "<": "lt",
        "<=": "lte",
    }
    return {"question": question, "op": operations.get(operator, operator), "value": _value(value)}


def _select(value: Any, path: str) -> Any:
    current = value
    for component in path.split("."):
        if isinstance(current, dict) and component in current:
            current = current[component]
        else:
            raise EpiqError("select_not_found", f"Output path not found: {path}")
    return current


def _ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_id") and isinstance(item, str):
                found.append(item)
            elif key.endswith("_ids") and isinstance(item, list):
                found.extend(str(identifier) for identifier in item)
            else:
                found.extend(_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_ids(item))
    return list(dict.fromkeys(found))


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return str(value.get("name", json.dumps(value, sort_keys=True)))
    if isinstance(value, list):
        return "; ".join(_display(item) for item in value)
    return str(value)


def _table(value: Any, command: str) -> str:
    """Render common read results as a compact terminal table."""
    if command in {"matrix", "query"} and isinstance(value, dict):
        questions = [str(item["name"]) for item in value.get("questions", [])]
        headers = [str(value.get("entity_kind", "Entity")), *questions]
        rows = []
        for row in value.get("rows", []):
            cells = []
            for question in questions:
                cell = row["cells"][question]
                if cell.get("state") in {"Unasked", "NotFound", "Contested"}:
                    rendered = str(cell["state"])
                elif cell.get("display_values"):
                    rendered = _display(cell["display_values"])
                elif cell.get("value") is not None:
                    rendered = _display(cell["value"])
                else:
                    rendered = _display(cell.get("values", []))
                cells.append(rendered)
            rows.append([str(row["name"]), *cells])
    elif command == "related" and isinstance(value, dict):
        headers = ["depth", "direction", "relationship", "from", "to"]
        rows = [
            [
                str(edge.get("depth", 1)),
                str(edge["direction"]),
                str(edge["question"]),
                str(edge["from"]["name"]),
                str(edge["to"]["name"]),
            ]
            for edge in value.get("edges", [])
        ]
    elif command == "timeline" and isinstance(value, dict):
        headers = ["as_of", "entity", "value", "confidence"]
        rows = [
            [
                str(item["as_of"]),
                str(item["entity_name"]),
                _display(item["value"]),
                str(item["confidence"]),
            ]
            for item in value.get("observations", [])
        ]
    elif command == "aggregate" and isinstance(value, dict):
        headers = ["group", str(value.get("operation", "value")), "count"]
        rows = [
            [str(item["group"]), _display(item["value"]), str(item["count"])]
            for item in value.get("groups", [])
        ]
    else:
        raise EpiqError(
            "table_format_unsupported",
            f"Table output is not available for {command}",
            "Use --format json for the complete machine-readable result.",
        )
    widths = [len(header) for header in headers]
    for row in rows:
        for index, item in enumerate(row):
            widths[index] = max(widths[index], len(item))
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(item.ljust(widths[index]) for index, item in enumerate(row)) for row in rows
    )
    return "\n".join(lines)


def _database(explicit: str | None) -> tuple[Path, str]:
    """Resolve the database using CLI, environment, workspace, then conventional default."""
    if explicit:
        return Path(explicit), "command_line"
    if environment := os.environ.get("EPIQ_DB"):
        return Path(environment), "environment"
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text())
        configured = config.get("database")
        if not isinstance(configured, str) or not configured:
            raise EpiqError("invalid_config", f"Invalid database setting in {CONFIG_PATH}")
        return Path(configured), "workspace"
    return DEFAULT_DB, "default"


def parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    root = argparse.ArgumentParser(
        prog="epiq", description="Evidence-backed agent research database"
    )
    root.add_argument("--db", help="SQLite project path; overrides EPIQ_DB and workspace config")
    root.add_argument("--actor", default="human:cli", help="Actor recorded for write commands")
    root.add_argument("--quiet", action="store_true", help="Suppress successful command output")
    root.add_argument("--select", help="Emit only a dotted output path, such as entity_id")
    root.add_argument("--format", choices=["json", "ids", "table"], default="json")
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize a project")
    init.add_argument("--name", required=True)

    use = commands.add_parser("use", help="Select the database for this workspace")
    use.add_argument("database")

    commands.add_parser("db", help="Show the currently selected database")

    apply = commands.add_parser("apply", help="Atomically converge a declarative project document")
    apply.add_argument("--input", required=True, help="JSON document, or - for standard input")

    seed = commands.add_parser("seed", help="Idempotently apply a fixture document")
    seed.add_argument("--input", required=True, help="JSON document, or - for standard input")

    commands.add_parser("doctor", help="Check SQLite integrity and event consistency")

    commands.add_parser("migration-plan", help="Inspect pending migrations without applying them")
    migrate = commands.add_parser("migrate", help="Apply pending database migrations explicitly")
    migrate.add_argument("--backup", help="Optional pre-migration SQLite backup path")

    backup = commands.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("--output", required=True)
    backup.add_argument("--force", action="store_true")

    export_bundle = commands.add_parser("export-bundle", help="Export a checksummed project bundle")
    export_bundle.add_argument("--output", required=True)
    export_bundle.add_argument("--force", action="store_true")

    import_bundle = commands.add_parser("import-bundle", help="Import a verified project bundle")
    import_bundle.add_argument("bundle")

    schema = commands.add_parser("schema", help="Describe row types and typed research fields")
    schema.add_argument("--kind")

    context = commands.add_parser("context", help="Emit compact current state for an agent")
    context.add_argument("--kind")
    context.add_argument("--budget", type=int, default=4000, help="Approximate token budget")

    gaps = commands.add_parser("gaps", help="List unanswered and unsuccessful research cells")
    gaps.add_argument("--kind", required=True)

    stale = commands.add_parser("stale", help="List cells whose temporal policy says stale")
    stale.add_argument("--kind", required=True)

    contradictions = commands.add_parser(
        "contradictions", help="List cells with incompatible active claims"
    )
    contradictions.add_argument("--kind", required=True)

    refresh_plan = commands.add_parser(
        "refresh-plan", help="Generate deterministic external-agent research tasks"
    )
    refresh_plan.add_argument("--kind", required=True)
    refresh_plan.add_argument(
        "--include", choices=["gaps", "stale", "contested", "all"], default="all"
    )

    search = commands.add_parser("search", help="Search entities, schema, evidence, and claims")
    search.add_argument("text")
    search.add_argument("--limit", type=int, default=50)

    entity = commands.add_parser("entity", help="Create an entity")
    entity.add_argument("kind")
    entity.add_argument("name", nargs="?")
    entity.add_argument("--attributes")
    entity.add_argument("--role", choices=["entity", "observation", "relation"], default="entity")
    entity.add_argument("--identity", help="JSON compound identity; makes creation idempotent")

    entity_alias = commands.add_parser("entity-alias", help="Add an alternate entity identity")
    entity_alias.add_argument("entity")
    entity_alias.add_argument("alias")

    merge_entities = commands.add_parser(
        "merge-entities", help="Merge a duplicate row into its surviving identity"
    )
    merge_entities.add_argument("source")
    merge_entities.add_argument("destination")
    merge_entities.add_argument("--reason", required=True)

    retire_entity = commands.add_parser(
        "retire-entity", help="Hide an entity from current projections without erasing history"
    )
    retire_entity.add_argument("entity")
    retire_entity.add_argument("--reason", required=True)

    restore_entity = commands.add_parser("restore-entity", help="Restore a retired entity")
    restore_entity.add_argument("entity")
    restore_entity.add_argument("--reason", required=True)

    question = commands.add_parser("question", help="Define a typed question")
    question.add_argument("name")
    question.add_argument("--for", dest="subject_kind", required=True)
    question.add_argument("--type", dest="value_type", required=True)
    question.add_argument("--definition", default="{}")

    retire_question = commands.add_parser(
        "retire-question", help="Remove a field from current projections without erasing history"
    )
    retire_question.add_argument("question")
    retire_question.add_argument("--reason", required=True)

    restore_question = commands.add_parser(
        "restore-question", help="Restore a previously retired field"
    )
    restore_question.add_argument("question")
    restore_question.add_argument("--reason", required=True)

    evolve_question = commands.add_parser(
        "evolve-question", help="Replace, refine, or split a field with explicit lineage"
    )
    evolve_question.add_argument("question")
    evolve_question.add_argument(
        "--relationship", choices=["replaces", "splits", "refines"], required=True
    )
    evolve_question.add_argument(
        "--replacement", action="append", required=True, help="JSON successor field definition"
    )
    evolve_question.add_argument("--reason", required=True)
    evolve_question.add_argument("--keep-predecessor", action="store_true")

    question_lineage = commands.add_parser(
        "question-lineage", help="Show predecessor and successor fields"
    )
    question_lineage.add_argument("question")

    evidence = commands.add_parser("evidence", help="Add a source and evidence fragment")
    evidence.add_argument(
        "--type",
        dest="source_type",
        choices=["web", "personal", "model", "report", "interview", "other"],
        default="web",
    )
    evidence.add_argument("--url")
    evidence.add_argument("--title", required=True)
    evidence.add_argument("--retrieved-at", required=True)
    evidence.add_argument("--locator", help="JSON locator such as page, section, or timestamp")
    evidence.add_argument("--source-entity", help="Entity represented by this source")
    excerpt_input = evidence.add_mutually_exclusive_group(required=True)
    excerpt_input.add_argument("--excerpt")
    excerpt_input.add_argument("--excerpt-file")

    assess_evidence = commands.add_parser(
        "assess-evidence", help="Append a quality assessment to immutable evidence"
    )
    assess_evidence.add_argument("evidence_id")
    assess_evidence.add_argument(
        "--status", choices=["accepted", "disputed", "invalid", "superseded"], required=True
    )
    assess_evidence.add_argument("--reason", required=True)

    evidence_history = commands.add_parser(
        "evidence-assessments", help="Read evidence assessment history"
    )
    evidence_history.add_argument("evidence_id")

    claim = commands.add_parser("assert", help="Assert an evidence-backed claim")
    claim.add_argument("--subject", required=True)
    claim.add_argument("--question", required=True)
    claim.add_argument("--value", required=True)
    claim.add_argument("--valid-from", required=True)
    claim.add_argument(
        "--evidence",
        action="append",
        required=True,
        help="Evidence ID; repeat for multiple sources",
    )
    claim.add_argument("--confidence", choices=["low", "medium", "high"], default="high")

    bulk_assert = commands.add_parser(
        "bulk-assert", help="Atomically assert a JSON array of evidence-backed claims"
    )
    bulk_assert.add_argument("--input", required=True, help="JSON file, or - for standard input")

    batch_write = commands.add_parser(
        "batch-write", help="Atomically add evidence and claims with local references"
    )
    batch_write.add_argument("--input", required=True, help="JSON file, or - for standard input")

    record = commands.add_parser(
        "record", help="Atomically record evidence and one or more supported answers"
    )
    record.add_argument("--subject")
    record.add_argument(
        "--source-type",
        "--type",
        dest="source_type",
        choices=["web", "personal", "model", "report", "interview", "other"],
        default="web",
    )
    record.add_argument("--url")
    record.add_argument("--source-title", "--title", dest="source_title", required=True)
    record.add_argument("--published-at")
    record.add_argument("--retrieved-at", required=True)
    record.add_argument("--locator", help="JSON locator such as page, section, or timestamp")
    record.add_argument("--source-entity", help="Entity represented by this source")
    record_excerpt = record.add_mutually_exclusive_group(required=True)
    record_excerpt.add_argument("--excerpt")
    record_excerpt.add_argument("--excerpt-file")
    record.add_argument("--valid-from", required=True)
    record.add_argument("--question")
    record.add_argument("--value")
    record.add_argument(
        "--answer",
        action="append",
        nargs=2,
        metavar=("QUESTION", "VALUE"),
        help="Question and value supported by this evidence; repeat for multiple answers",
    )
    record.add_argument(
        "--cell",
        action="append",
        nargs=3,
        metavar=("SUBJECT", "QUESTION", "VALUE"),
        help="Subject, question, and value supported by this evidence; repeat across rows",
    )
    record.add_argument("--confidence", choices=["low", "medium", "high"], default="high")
    record.add_argument(
        "--temporal-basis", choices=["observed", "source", "unknown"], default="observed"
    )

    propose_claim = commands.add_parser(
        "propose-claim", help="Stage a validated claim for human review"
    )
    propose_claim.add_argument("--subject", required=True)
    propose_claim.add_argument("--question", required=True)
    propose_claim.add_argument("--value", required=True)
    propose_claim.add_argument("--valid-from", required=True)
    propose_claim.add_argument("--evidence", action="append", required=True)
    propose_claim.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    propose_claim.add_argument(
        "--temporal-basis", choices=["observed", "source", "unknown"], default="observed"
    )
    propose_claim.add_argument("--rationale", default="")

    proposals = commands.add_parser("claim-proposals", help="Read the durable claim review queue")
    proposals.add_argument(
        "--status", choices=["pending", "approved", "rejected", "all"], default="pending"
    )

    review_proposals = commands.add_parser(
        "review-claims", help="Atomically approve or reject claim proposals"
    )
    review_proposals.add_argument("proposal_id", nargs="+")
    review_proposals.add_argument("--decision", choices=["approved", "rejected"], required=True)
    review_proposals.add_argument("--reason", required=True)

    retract = commands.add_parser("retract", help="Retract a claim")
    retract.add_argument("claim_id")
    retract.add_argument("--reason", required=True)

    validity_end = commands.add_parser(
        "end-validity", help="Record when an asserted fact stopped being true"
    )
    validity_end.add_argument("claim_id")
    validity_end.add_argument("--valid-to", required=True)
    validity_end.add_argument("--reason", required=True)

    supersede = commands.add_parser("supersede", help="Atomically replace an active claim")
    supersede.add_argument("claim_id")
    supersede.add_argument("--value", required=True)
    supersede.add_argument("--valid-from", required=True)
    supersede.add_argument("--evidence", action="append", required=True)
    supersede.add_argument("--reason", required=True)
    supersede.add_argument("--confidence", choices=["low", "medium", "high"], default="high")
    supersede.add_argument(
        "--temporal-basis", choices=["observed", "source", "unknown"], default="observed"
    )

    challenge = commands.add_parser(
        "challenge-question", help="Record that a question cannot represent an observation"
    )
    challenge.add_argument("question")
    challenge.add_argument("--problem", choices=sorted(QUESTION_CHALLENGE_PROBLEMS), required=True)
    challenge.add_argument("--explanation", required=True)
    challenge.add_argument("--example-entity")
    challenge.add_argument("--evidence", action="append", default=[])
    challenge.add_argument(
        "--proposed-replacement", help="JSON object with at least name and value_type"
    )

    challenges = commands.add_parser(
        "question-challenges", help="List open or historical question challenges"
    )
    challenges.add_argument("--question")
    challenges.add_argument("--status", choices=["open", "resolved", "dismissed"])

    resolve_challenge = commands.add_parser(
        "resolve-question-challenge", help="Resolve or dismiss a schema challenge"
    )
    resolve_challenge.add_argument("challenge_id")
    resolve_challenge.add_argument("--status", choices=["resolved", "dismissed"], required=True)
    resolve_challenge.add_argument("--resolution", required=True)

    season = commands.add_parser("season-record", help="Derive a season record with lineage")
    season.add_argument("season")
    season.add_argument("--known-at")
    season.add_argument("--valid-at")

    history = commands.add_parser("history", help="Read the append-only event history")
    history.add_argument("--type", dest="event_type")

    check = commands.add_parser("check", help="Parse and statically check an EpiQL file")
    check.add_argument("file")

    demo = commands.add_parser("demo", help="Load a reproducible example")
    demo.add_argument("name", choices=["patriots"])

    matrix = commands.add_parser("matrix", help="Project entities and questions into cells")
    matrix.add_argument("--kind", required=True)
    matrix.add_argument("--questions", help="Comma-separated question names")
    matrix.add_argument("--known-at")
    matrix.add_argument("--valid-at")

    query = commands.add_parser("query", help="Filter rows with structured JSON predicates")
    query.add_argument("--kind", required=True)
    query.add_argument("--where", action="append", default=[], help="JSON predicate")
    query.add_argument("--known-at")
    query.add_argument("--valid-at")

    aggregate = commands.add_parser("aggregate", help="Group and summarize current numeric values")
    aggregate.add_argument("--kind", required=True)
    aggregate.add_argument("--question", required=True)
    aggregate.add_argument("--op", choices=["count", "sum", "avg", "min", "max"], required=True)
    aggregate.add_argument("--group-by")

    dossier = commands.add_parser("dossier", help="Generate a sourced entity dossier")
    dossier.add_argument("entity")

    related = commands.add_parser("related", help="Traverse typed entity relationships")
    related.add_argument("entity")
    related.add_argument("--via")
    related.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    related.add_argument(
        "--depth", type=int, default=1, help="Maximum relationship traversal depth"
    )

    timeline = commands.add_parser("timeline", help="Generate a field timeline across a table")
    timeline.add_argument("--kind", required=True)
    timeline.add_argument("--question", required=True)

    delta = commands.add_parser("delta", help="Report events since a baseline or prior delta")
    delta.add_argument("--since-seq", type=int)

    excel = commands.add_parser("export-xlsx", help="Export a projection as an Excel workbook")
    excel.add_argument("--kind", required=True)
    excel.add_argument("--output", required=True)
    excel.add_argument("--questions", help="Comma-separated question names")
    excel.add_argument("--known-at")
    excel.add_argument("--valid-at")

    html = commands.add_parser("export-html", help="Export an interactive HTML explorer")
    html.add_argument("--output", required=True)
    html.add_argument("--kind")

    corpus = commands.add_parser("import-cham", help="Import an earlier Cham JSON corpus")
    corpus.add_argument("--entities", required=True)
    corpus.add_argument("--evidence", required=True)
    corpus.add_argument("--claims", required=True)

    not_found = commands.add_parser("not-found", help="Record a search with no sufficient evidence")
    not_found.add_argument("--subject", required=True)
    not_found.add_argument("--question", required=True)
    not_found.add_argument("--query", required=True)
    not_found.add_argument("--notes", required=True)

    derive = commands.add_parser(
        "derive-distribution", help="Derive an empirical distribution from numeric claims"
    )
    derive.add_argument("--subject", required=True)
    derive.add_argument("--question", required=True)
    derive.add_argument(
        "--input-claim",
        action="append",
        required=True,
        help="Input claim ID; repeat or provide comma-separated IDs",
    )
    derive.add_argument("--weights", help="Optional JSON array of weights summing to 1")
    derive.add_argument("--valid-from", required=True)
    derive.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    derive_claim = commands.add_parser(
        "derive", help="Persist a computed claim with input-claim lineage"
    )
    derive_claim.add_argument("--subject", required=True)
    derive_claim.add_argument("--question", required=True)
    derive_claim.add_argument(
        "--operation",
        choices=["sum", "avg", "min", "max", "count", "weighted_avg", "linear"],
        required=True,
    )
    derive_claim.add_argument("--input-claim", action="append")
    derive_claim.add_argument(
        "--input-cell",
        action="append",
        nargs=2,
        metavar=("SUBJECT", "QUESTION"),
        help="Resolve active claim(s) from a cell; repeat as needed",
    )
    derive_claim.add_argument("--parameters", default="{}", help="JSON operation parameters")
    derive_claim.add_argument(
        "--weight-cell",
        action="append",
        nargs=2,
        metavar=("SUBJECT", "QUESTION"),
        help="Resolve a numeric claim as a weighted_avg weight; repeat in input order",
    )
    derive_claim.add_argument("--valid-from", required=True)
    derive_claim.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    materialize = commands.add_parser(
        "materialize", help="Materialize question formulas as derived claims"
    )
    materialize.add_argument("--kind", required=True)
    materialize.add_argument("--subject", action="append", help="Limit to an entity; repeatable")
    materialize.add_argument("--valid-from", required=True)
    propagate = commands.add_parser(
        "propagate", help="Copy a claim through a typed relationship with derived lineage"
    )
    propagate.add_argument("--subject", required=True)
    propagate.add_argument("--via", required=True)
    propagate.add_argument("--question", required=True, help="Question on the related entity")
    propagate.add_argument("--to-question", required=True, help="Target question on subject")
    propagate.add_argument("--direction", choices=["incoming", "outgoing"], default="outgoing")
    propagate.add_argument("--depth", type=int, default=1)
    propagate.add_argument("--valid-from", required=True)
    propagate.add_argument("--confidence", choices=["low", "medium", "high"], default="medium")
    stale_derivations = commands.add_parser(
        "stale-derivations", help="Find derived claims whose dependencies changed"
    )
    stale_derivations.add_argument("--kind")
    return root


def run(args: argparse.Namespace) -> dict[str, Any] | list[dict[str, Any]]:
    """Execute one parsed command."""
    if args.command == "use":
        database = Path(args.database).expanduser().resolve()
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({"database": str(database)}, indent=2) + "\n")
        return {
            "ok": True,
            "database": str(database),
            "config": str(CONFIG_PATH),
            "exists": database.exists(),
        }
    database, database_source = _database(args.db)
    if args.command == "db":
        return {
            "ok": True,
            "database": str(database),
            "source": database_source,
            "exists": database.exists(),
        }
    store = Store(database)
    if args.command == "init":
        store.initialize(args.name)
        return {"ok": True, "database": str(database), "name": args.name}
    if args.command == "import-bundle":
        imported = Store.import_bundle(args.bundle, database)
        return {"ok": True, "database": str(database), "project": imported.overview()["project"]}
    if args.command in {"apply", "seed"}:
        document = _json_file(args.input)
        if not isinstance(document, dict):
            raise EpiqError("invalid_apply_document", "Apply input must be a JSON object")
        if not database.exists():
            project = document.get("project", {})
            name = project.get("name") if isinstance(project, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise EpiqError(
                    "project_name_required",
                    "A new database requires project.name in the apply document",
                )
            store.initialize(name)
        return store.apply_document(document, args.actor)
    if not database.exists():
        raise EpiqError(
            "project_not_found",
            f"Database does not exist: {database}",
            "Run: epiq init --name 'My research space' or select another database with epiq use",
        )
    if args.command == "migration-plan":
        return store.migration_plan()
    if args.command == "migrate":
        return store.migrate(args.backup)
    if args.command == "doctor":
        return store.doctor()
    if args.command == "backup":
        output = store.backup(args.output, args.force)
        return {"ok": True, "database": str(database), "backup": str(output)}
    if args.command == "export-bundle":
        output = store.export_bundle(args.output, args.force)
        return {"ok": True, "database": str(database), "bundle": str(output)}
    if args.command == "schema":
        overview = store.overview()
        kinds = [item["kind"] for item in overview["entity_kinds"]]
        selected = [args.kind] if args.kind else kinds
        return {
            "project": overview["project"],
            "value_types": [
                "String",
                "Date",
                "DateTime",
                "Year",
                "Interval[Date]",
                "Int",
                "Float",
                "Probability",
                "Bool",
                "Json",
                "Enum[a,b,c]",
                "Distribution[Float]",
                "Distribution[Enum[a,b,c]]",
                "Ref[EntityKind]",
                "Quantity[unit]",
            ],
            "tables": [
                {
                    "entity_kind": kind,
                    "questions": store.matrix(kind)["questions"],
                }
                for kind in selected
            ],
        }
    if args.command == "context":
        if args.budget < 100:
            raise EpiqError("invalid_budget", "Context budget must be at least 100 tokens")
        overview = store.overview()
        kinds = [item["kind"] for item in overview["entity_kinds"]]
        selected = [args.kind] if args.kind else kinds
        tables = [store.matrix(kind) for kind in selected]
        result: dict[str, Any] = {
            "project": overview["project"],
            "tables": tables,
            "truncated": False,
        }
        encoded = json.dumps(result, sort_keys=True)
        character_budget = args.budget * 4
        if len(encoded) > character_budget:
            compact_tables = []
            used = 0
            for table in tables:
                compact = {
                    "entity_kind": table["entity_kind"],
                    "questions": [
                        {
                            "name": question["name"],
                            "value_type": question["value_type"],
                            "definition": question["definition"],
                        }
                        for question in table["questions"]
                    ],
                    "rows": [],
                }
                for row in table["rows"]:
                    candidate = {
                        "entity_id": row["entity_id"],
                        "name": row["name"],
                        "cells": {
                            name: {
                                "state": cell["state"],
                                "value": cell.get("value"),
                                "confidence": (
                                    cell["lineage"][0]["confidence"]
                                    if cell.get("lineage")
                                    else None
                                ),
                            }
                            for name, cell in row["cells"].items()
                        },
                    }
                    size = len(json.dumps(candidate, sort_keys=True))
                    if used + size > character_budget:
                        break
                    compact["rows"].append(candidate)
                    used += size
                compact_tables.append(compact)
            result = {
                "project": overview["project"],
                "tables": compact_tables,
                "truncated": True,
                "approximate_token_budget": args.budget,
            }
        return result
    if args.command in {"gaps", "stale"}:
        projection = store.matrix(args.kind)
        cells = []
        for row in projection["rows"]:
            for question in projection["questions"]:
                cell = row["cells"][question["name"]]
                include = (
                    cell["state"] in {"Unasked", "NotFound"}
                    if args.command == "gaps"
                    else cell.get("temporal", {}).get("freshness") == "stale"
                )
                if include:
                    cells.append(
                        {
                            "entity_id": row["entity_id"],
                            "entity_name": row["name"],
                            "question_id": question["question_id"],
                            "question": question["name"],
                            "state": cell["state"],
                            "temporal": cell.get("temporal"),
                        }
                    )
        return {"entity_kind": args.kind, "count": len(cells), "cells": cells}
    if args.command == "contradictions":
        projection = store.matrix(args.kind)
        cells = [
            {
                "entity_id": row["entity_id"],
                "entity_name": row["name"],
                "question_id": question["question_id"],
                "question": question["name"],
                "values": row["cells"][question["name"]]["values"],
                "lineage": row["cells"][question["name"]]["lineage"],
            }
            for row in projection["rows"]
            for question in projection["questions"]
            if row["cells"][question["name"]]["state"] == "Contested"
        ]
        return {"entity_kind": args.kind, "count": len(cells), "cells": cells}
    if args.command == "refresh-plan":
        projection = store.matrix(args.kind)
        tasks = []
        for row in projection["rows"]:
            for question in projection["questions"]:
                cell = row["cells"][question["name"]]
                reasons = []
                if cell["state"] in {"Unasked", "NotFound"}:
                    reasons.append("gap")
                if cell.get("temporal", {}).get("freshness") == "stale":
                    reasons.append("stale")
                if cell["state"] == "Contested":
                    reasons.append("contested")
                allowed = (
                    reasons
                    if args.include == "all"
                    else [reason for reason in reasons if reason == args.include.rstrip("s")]
                )
                if not allowed:
                    continue
                label = str(question["definition"].get("label", question["name"]))
                tasks.append(
                    {
                        "task_key": f"{row['entity_id']}:{question['question_id']}",
                        "entity_kind": args.kind,
                        "entity_id": row["entity_id"],
                        "entity_name": row["name"],
                        "question_id": question["question_id"],
                        "question": question["name"],
                        "value_type": question["value_type"],
                        "reasons": allowed,
                        "suggested_query": f'"{row["name"]}" {label}',
                        "research_guidance": question["definition"].get("research_guidance", ""),
                        "existing_values": cell.get("values", []),
                        "existing_source_urls": list(
                            dict.fromkeys(
                                lineage["source"]["url"] for lineage in cell.get("lineage", [])
                            )
                        ),
                    }
                )
        return {"entity_kind": args.kind, "count": len(tasks), "tasks": tasks}
    if args.command == "search":
        results = store.search(args.text, args.limit)
        return {"query": args.text, "count": len(results), "results": results}
    if args.command == "entity":
        identity = _attributes(args.identity) if args.identity else None
        name = _entity_name(args.kind, args.name, identity)
        entity_id = store.add_entity(
            args.kind,
            name,
            _attributes(args.attributes),
            args.actor,
            args.role,
            identity,
        )
        return {"ok": True, "entity_id": entity_id}
    if args.command == "entity-alias":
        alias_id = store.add_entity_alias(args.entity, args.alias, args.actor)
        return {"ok": True, "alias_id": alias_id}
    if args.command == "merge-entities":
        entity_id = store.merge_entities(args.source, args.destination, args.reason, args.actor)
        return {"ok": True, "entity_id": entity_id, "status": "merged"}
    if args.command in {"retire-entity", "restore-entity"}:
        visible = args.command == "restore-entity"
        entity_id = store.set_entity_visibility(args.entity, visible, args.reason, args.actor)
        return {
            "ok": True,
            "entity_id": entity_id,
            "status": "active" if visible else "retired",
        }
    if args.command == "question":
        definition = _attributes(args.definition)
        question_id = store.add_question(
            args.name, args.subject_kind, args.value_type, definition, args.actor
        )
        return {"ok": True, "question_id": question_id}
    if args.command in {"retire-question", "restore-question"}:
        visible = args.command == "restore-question"
        question_id = store.set_question_visibility(args.question, visible, args.reason, args.actor)
        return {
            "ok": True,
            "question_id": question_id,
            "status": "active" if visible else "retired",
        }
    if args.command == "evolve-question":
        replacements = [_attributes(item) for item in args.replacement]
        successor_ids = store.evolve_question(
            args.question,
            replacements,
            args.relationship,
            args.reason,
            args.actor,
            not args.keep_predecessor,
        )
        return {"ok": True, "successor_question_ids": successor_ids}
    if args.command == "question-lineage":
        return store.question_lineage(args.question)
    if args.command == "evidence":
        excerpt = Path(args.excerpt_file).read_text() if args.excerpt_file else args.excerpt
        if args.source_type == "web" and not args.url:
            raise EpiqError("source_url_required", "A web source requires --url")
        locator = args.url or (
            f"urn:epiq:{args.source_type}:"
            + hashlib.sha256(f"{args.title}\n{excerpt}".encode()).hexdigest()[:24]
        )
        source_id, evidence_id = store.add_evidence(
            locator,
            args.title,
            args.retrieved_at,
            excerpt,
            args.actor,
            source_type=args.source_type,
            locator=_attributes(args.locator) if args.locator else None,
            source_entity=args.source_entity,
        )
        return {"ok": True, "source_id": source_id, "evidence_id": evidence_id}
    if args.command == "assess-evidence":
        assessment_id = store.assess_evidence(
            args.evidence_id, args.status, args.reason, args.actor
        )
        return {"ok": True, "assessment_id": assessment_id, "status": args.status}
    if args.command == "evidence-assessments":
        items = store.evidence_assessments(args.evidence_id)
        return {"count": len(items), "assessments": items}
    if args.command == "assert":
        claim_id = store.assert_claim(
            args.subject,
            args.question,
            _value(args.value),
            args.valid_from,
            args.evidence,
            args.actor,
            confidence=args.confidence,
        )
        return {"ok": True, "claim_id": claim_id}
    if args.command == "bulk-assert":
        items = _json_file(args.input)
        if not isinstance(items, list):
            raise EpiqError("invalid_batch", "Bulk assertion input must be a JSON array")
        claim_ids = store.assert_claims_bulk(items, args.actor)
        return {"ok": True, "count": len(claim_ids), "claim_ids": claim_ids}
    if args.command == "batch-write":
        operations = _json_file(args.input)
        if not isinstance(operations, list):
            raise EpiqError("invalid_batch", "Batch write input must be a JSON array")
        results = store.write_batch(operations, args.actor)
        return {"ok": True, "count": len(results), "results": results}
    if args.command == "record":
        single_supplied = args.question is not None or args.value is not None
        if single_supplied and (args.question is None or args.value is None):
            raise EpiqError(
                "incomplete_answer",
                "--question and --value must be provided together",
                "Use both flags for one answer, or repeat --answer QUESTION VALUE.",
            )
        if single_supplied and (args.answer or args.cell):
            raise EpiqError(
                "mixed_answer_syntax",
                "Do not combine --question/--value with --answer",
                "Use --question and --value once, or repeat --answer for one or more answers.",
            )
        if args.cell and args.answer:
            raise EpiqError(
                "mixed_answer_syntax",
                "Do not combine --cell with --answer",
                "Use --cell SUBJECT QUESTION VALUE for multi-subject evidence.",
            )
        if (single_supplied or args.answer) and not args.subject:
            raise EpiqError(
                "subject_required", "--subject is required with --question/--value or --answer"
            )
        cells = (
            [(args.subject, args.question, args.value)]
            if single_supplied
            else (
                [(args.subject, question, value) for question, value in (args.answer or [])]
                if args.answer
                else [(subject, question, value) for subject, question, value in (args.cell or [])]
            )
        )
        if not cells:
            raise EpiqError(
                "answer_required",
                "Record requires at least one answer",
                "Use --question/--value, --answer QUESTION VALUE, "
                "or --cell SUBJECT QUESTION VALUE.",
            )
        excerpt = Path(args.excerpt_file).read_text() if args.excerpt_file else args.excerpt
        if args.source_type == "web" and not args.url:
            raise EpiqError("source_url_required", "A web source requires --url")
        locator = args.url or (
            f"urn:epiq:{args.source_type}:"
            + hashlib.sha256(f"{args.source_title}\n{excerpt}".encode()).hexdigest()[:24]
        )
        evidence_ref = "record_evidence"
        operations: list[dict[str, Any]] = [
            {
                "op": "evidence.add",
                "ref": evidence_ref,
                "url": locator,
                "title": args.source_title,
                "published_at": args.published_at,
                "retrieved_at": args.retrieved_at,
                "excerpt": excerpt,
                "source_type": args.source_type,
                "locator": _attributes(args.locator) if args.locator else {},
                "source_entity": args.source_entity,
            }
        ]
        operations.extend(
            {
                "op": "claim.assert",
                "subject": subject,
                "question": question,
                "value": _value(value),
                "valid_from": args.valid_from,
                "evidence_refs": [evidence_ref],
                "confidence": args.confidence,
                "temporal_basis": args.temporal_basis,
            }
            for subject, question, value in cells
        )
        results = store.write_batch(operations, args.actor)
        evidence_result = results[0]
        claim_ids = [result["claim_id"] for result in results[1:]]
        return {
            "ok": True,
            "source_id": evidence_result["source_id"],
            "evidence_id": evidence_result["evidence_id"],
            "claim_ids": claim_ids,
            "answer_count": len(claim_ids),
        }
    if args.command == "propose-claim":
        proposal_id = store.propose_claim(
            args.subject,
            args.question,
            _value(args.value),
            args.valid_from,
            args.evidence,
            args.actor,
            args.confidence,
            args.temporal_basis,
            args.rationale,
        )
        return {"ok": True, "proposal_id": proposal_id, "status": "pending"}
    if args.command == "claim-proposals":
        status = None if args.status == "all" else args.status
        items = store.claim_proposals(status)
        return {"count": len(items), "proposals": items}
    if args.command == "review-claims":
        results = store.review_claim_proposals(
            args.proposal_id, args.decision, args.reason, args.actor
        )
        return {"ok": True, "count": len(results), "results": results}
    if args.command == "retract":
        store.close_claim(args.claim_id, "retracted", args.reason, args.actor)
        return {"ok": True, "claim_id": args.claim_id, "status": "retracted"}
    if args.command == "end-validity":
        store.end_claim_validity(args.claim_id, args.valid_to, args.reason, args.actor)
        return {"ok": True, "claim_id": args.claim_id, "valid_to": args.valid_to}
    if args.command == "supersede":
        replacement_id = store.supersede_claim(
            args.claim_id,
            _value(args.value),
            args.valid_from,
            args.evidence,
            args.reason,
            args.actor,
            args.confidence,
            args.temporal_basis,
        )
        return {
            "ok": True,
            "claim_id": args.claim_id,
            "status": "superseded",
            "replacement_claim_id": replacement_id,
        }
    if args.command == "challenge-question":
        replacement = _attributes(args.proposed_replacement) if args.proposed_replacement else None
        challenge_id = store.challenge_question(
            args.question,
            args.problem,
            args.explanation,
            args.actor,
            args.example_entity,
            args.evidence,
            replacement,
        )
        return {"ok": True, "challenge_id": challenge_id, "status": "open"}
    if args.command == "question-challenges":
        return store.question_challenges(args.question, args.status)
    if args.command == "resolve-question-challenge":
        store.resolve_question_challenge(
            args.challenge_id, args.status, args.resolution, args.actor
        )
        return {"ok": True, "challenge_id": args.challenge_id, "status": args.status}
    if args.command == "season-record":
        return store.season_record(args.season, args.known_at, args.valid_at)
    if args.command == "history":
        events = store.history()
        return [
            event
            for event in events
            if not args.event_type or event["event_type"] == args.event_type
        ]
    if args.command == "check":
        return {"ok": True, "program": describe(parse(Path(args.file).read_text()))}
    if args.command == "demo":
        return {"ok": True, "demo": args.name, **load_patriots(store, args.actor)}
    if args.command == "matrix":
        questions = args.questions.split(",") if args.questions else None
        return store.matrix(args.kind, questions, args.known_at, args.valid_at)
    if args.command == "query":
        predicates = [_predicate(item) for item in args.where]
        return store.query_rows(args.kind, predicates, args.known_at, args.valid_at)
    if args.command == "aggregate":
        projection = store.matrix(args.kind)
        question_names = {str(item["name"]) for item in projection["questions"]}
        if args.question not in question_names:
            raise EpiqError("question_not_found", f"Question not found: {args.question}")
        if args.group_by and args.group_by not in question_names:
            raise EpiqError("question_not_found", f"Question not found: {args.group_by}")
        grouped: dict[str, list[Any]] = {}
        for row in projection["rows"]:
            cell = row["cells"][args.question]
            values = cell.get("values", [])
            if not values:
                continue
            keys = ["all"]
            if args.group_by:
                group_cell = row["cells"][args.group_by]
                keys = [_display(item) for item in group_cell.get("display_values", [])]
                if not keys:
                    keys = [_display(item) for item in group_cell.get("values", [])]
                if not keys:
                    keys = ["Unasked"]
            for key in keys:
                grouped.setdefault(key, []).extend(values)
        groups = []
        for key, values in sorted(grouped.items()):
            if args.op == "count":
                result: Any = len(values)
            else:
                numeric = all(
                    isinstance(item, (int, float)) and not isinstance(item, bool) for item in values
                )
                if not numeric:
                    raise EpiqError(
                        "non_numeric_aggregate",
                        f"{args.question} contains non-numeric values",
                    )
                result = {
                    "sum": sum,
                    "avg": lambda items: sum(items) / len(items),
                    "min": min,
                    "max": max,
                }[args.op](values)
            groups.append({"group": key, "value": result, "count": len(values)})
        return {
            "entity_kind": args.kind,
            "question": args.question,
            "operation": args.op,
            "groups": groups,
        }
    if args.command == "dossier":
        return store.dossier(args.entity)
    if args.command == "related":
        return store.related(args.entity, args.via, args.direction, args.depth)
    if args.command == "timeline":
        return store.timeline(args.kind, args.question)
    if args.command == "delta":
        return store.delta_report(args.actor, args.since_seq)
    if args.command == "export-xlsx":
        questions = args.questions.split(",") if args.questions else None
        matrix = store.matrix(args.kind, questions, args.known_at, args.valid_at)
        output = write_xlsx(matrix, args.output)
        return {
            "ok": True,
            "output": str(output),
            "entity_kind": args.kind,
            "entities": len(matrix["rows"]),
            "questions": len(matrix["questions"]),
        }
    if args.command == "export-html":
        output = write_html(store, args.output, args.kind)
        return {"ok": True, "output": str(output), "entity_kind": args.kind}
    if args.command == "import-cham":
        return {
            "ok": True,
            "imported": import_cham_corpus(
                store, args.entities, args.evidence, args.claims, args.actor
            ),
        }
    if args.command == "not-found":
        task_id = store.record_not_found(
            args.subject, args.question, args.query, args.notes, args.actor
        )
        return {"ok": True, "task_id": task_id, "state": "NotFound"}
    if args.command == "derive-distribution":
        input_claims = [
            claim_id.strip()
            for group in args.input_claim
            for claim_id in group.split(",")
            if claim_id.strip()
        ]
        weights = _value(args.weights) if args.weights else None
        if weights is not None and not isinstance(weights, list):
            raise EpiqError("invalid_weights", "--weights must be a JSON array")
        claim_id = store.derive_distribution(
            args.subject,
            args.question,
            input_claims,
            args.valid_from,
            args.actor,
            weights,
            args.confidence,
        )
        return {"ok": True, "claim_id": claim_id, "input_claim_ids": input_claims}
    if args.command == "derive":
        input_claims = [
            claim_id.strip()
            for group in (args.input_claim or [])
            for claim_id in group.split(",")
            if claim_id.strip()
        ]
        for subject, question in args.input_cell or []:
            input_claims.extend(store.active_claim_ids(subject, question))
        weight_claims: list[str] = []
        for subject, question in args.weight_cell or []:
            resolved = store.active_claim_ids(subject, question)
            if len(resolved) != 1:
                raise EpiqError(
                    "ambiguous_weight_cell",
                    f"Weight cell {subject} / {question} must have exactly one active claim",
                )
            weight_claims.extend(resolved)
        claim_id = store.derive_claim(
            args.subject,
            args.question,
            args.operation,
            input_claims,
            args.valid_from,
            args.actor,
            _attributes(args.parameters),
            args.confidence,
            weight_claims,
        )
        return {
            "ok": True,
            "claim_id": claim_id,
            "operation": args.operation,
            "input_claim_ids": input_claims,
            "parameter_claim_ids": weight_claims,
        }
    if args.command == "materialize":
        return store.materialize_formulas(args.kind, args.valid_from, args.actor, args.subject)
    if args.command == "propagate":
        claim_id, source = store.propagate_claim(
            args.subject,
            args.via,
            args.question,
            args.to_question,
            args.direction,
            args.depth,
            args.valid_from,
            args.actor,
            args.confidence,
        )
        return {"ok": True, "claim_id": claim_id, "source_entity": source}
    if args.command == "stale-derivations":
        return store.stale_derivations(args.kind)
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    """Run Epiq and emit exactly one JSON value."""
    try:
        args = parser().parse_args(argv)
        result = run(args)
        if args.quiet:
            return
        if args.select:
            result = _select(result, args.select)
        if args.format == "ids":
            result = _ids(result)
        if args.format == "table":
            print(_table(result, args.command))
        else:
            _emit(result)
    except EpiqError as exc:
        print(json.dumps({"error": exc.as_dict()}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2) from None
    except (json.JSONDecodeError, OSError) as exc:
        print(
            json.dumps({"error": {"code": "invalid_input", "message": str(exc)}}, sort_keys=True),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except sqlite3.Error as exc:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "database_error",
                        "message": str(exc),
                        "suggestion": "Run `epiq doctor` and restore a recent backup if needed.",
                    }
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
