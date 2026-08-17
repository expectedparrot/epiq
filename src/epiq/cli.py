"""JSON-first command-line interface for Epiq."""

from __future__ import annotations

import argparse
import json
import os
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


def _json_file(path: str) -> Any:
    text = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise EpiqError("invalid_json", f"Invalid JSON input: {error}") from error


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
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize a project")
    init.add_argument("--name", required=True)

    use = commands.add_parser("use", help="Select the database for this workspace")
    use.add_argument("database")

    commands.add_parser("db", help="Show the currently selected database")

    commands.add_parser("doctor", help="Check SQLite integrity and event consistency")

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
    entity.add_argument("name")
    entity.add_argument("--attributes")

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
    evidence.add_argument("--url", required=True)
    evidence.add_argument("--title", required=True)
    evidence.add_argument("--retrieved-at", required=True)
    evidence.add_argument("--excerpt", required=True)

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

    dossier = commands.add_parser("dossier", help="Generate a sourced entity dossier")
    dossier.add_argument("entity")

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
    if not database.exists():
        raise EpiqError(
            "project_not_found",
            f"Database does not exist: {database}",
            "Run: epiq init --name 'My research space' or select another database with epiq use",
        )
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
        entity_id = store.add_entity(args.kind, args.name, _attributes(args.attributes), args.actor)
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
        source_id, evidence_id = store.add_evidence(
            args.url, args.title, args.retrieved_at, args.excerpt, args.actor
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
        predicates = [_attributes(item) for item in args.where]
        return store.query_rows(args.kind, predicates, args.known_at, args.valid_at)
    if args.command == "dossier":
        return store.dossier(args.entity)
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
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> None:
    """Run Epiq and emit exactly one JSON value."""
    try:
        _emit(run(parser().parse_args(argv)))
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
