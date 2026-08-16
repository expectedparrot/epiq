"""JSON-first command-line interface for Epiq."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .demo import load_patriots
from .dsl import describe, parse
from .errors import EpiqError
from .html import write_html
from .importers import import_cham_corpus
from .store import Store
from .xlsx import write_xlsx


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


def parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    root = argparse.ArgumentParser(
        prog="epiq", description="Evidence-backed agent research database"
    )
    root.add_argument("--db", default=".epiq/epiq.sqlite", help="SQLite project path")
    root.add_argument("--actor", default="human:cli", help="Actor recorded for write commands")
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="Initialize a project")
    init.add_argument("--name", required=True)

    entity = commands.add_parser("entity", help="Create an entity")
    entity.add_argument("kind")
    entity.add_argument("name")
    entity.add_argument("--attributes")

    question = commands.add_parser("question", help="Define a typed question")
    question.add_argument("name")
    question.add_argument("--for", dest="subject_kind", required=True)
    question.add_argument("--type", dest="value_type", required=True)
    question.add_argument("--definition", default="{}")

    evidence = commands.add_parser("evidence", help="Add a source and evidence fragment")
    evidence.add_argument("--url", required=True)
    evidence.add_argument("--title", required=True)
    evidence.add_argument("--retrieved-at", required=True)
    evidence.add_argument("--excerpt", required=True)

    claim = commands.add_parser("assert", help="Assert an evidence-backed claim")
    claim.add_argument("--subject", required=True)
    claim.add_argument("--question", required=True)
    claim.add_argument("--value", required=True)
    claim.add_argument("--valid-from", required=True)
    claim.add_argument("--evidence", required=True)
    claim.add_argument("--confidence", choices=["low", "medium", "high"], default="high")

    retract = commands.add_parser("retract", help="Retract a claim")
    retract.add_argument("claim_id")
    retract.add_argument("--reason", required=True)

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
    return root


def run(args: argparse.Namespace) -> dict[str, Any] | list[dict[str, Any]]:
    """Execute one parsed command."""
    store = Store(args.db)
    if args.command == "init":
        store.initialize(args.name)
        return {"ok": True, "database": str(Path(args.db)), "name": args.name}
    if not Path(args.db).exists():
        raise EpiqError(
            "project_not_found",
            f"Database does not exist: {args.db}",
            f"Run: epiq --db {args.db} init --name 'My research space'",
        )
    if args.command == "entity":
        entity_id = store.add_entity(args.kind, args.name, _attributes(args.attributes), args.actor)
        return {"ok": True, "entity_id": entity_id}
    if args.command == "question":
        definition = _attributes(args.definition)
        question_id = store.add_question(
            args.name, args.subject_kind, args.value_type, definition, args.actor
        )
        return {"ok": True, "question_id": question_id}
    if args.command == "evidence":
        source_id, evidence_id = store.add_evidence(
            args.url, args.title, args.retrieved_at, args.excerpt, args.actor
        )
        return {"ok": True, "source_id": source_id, "evidence_id": evidence_id}
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
    if args.command == "retract":
        store.close_claim(args.claim_id, "retracted", args.reason, args.actor)
        return {"ok": True, "claim_id": args.claim_id, "status": "retracted"}
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
