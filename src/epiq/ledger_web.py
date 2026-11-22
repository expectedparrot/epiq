"""FastAPI surface backed exclusively by the authoritative Git ledger."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .errors import EpiqError
from .ledger_api import LedgerDatabase
from .ledger_diagnostics import diagnose
from .research import (
    OpenAIResearchRunner,
    PiWorkspaceAgentRunner,
    ResearchRunner,
    WorkspaceAgentRunner,
)
from .temporal import research_instant


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    actor: str = "human:web"


class ProjectOpen(BaseModel):
    project_id: str = Field(min_length=1)


class KindCreate(BaseModel):
    kind: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class EntityCreate(BaseModel):
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    actor: str = "human:web"
    idempotency_key: str | None = None


class QuestionCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    subject_kind: str = Field(min_length=1)
    value_type: str = Field(min_length=1)
    definition: dict[str, Any] = Field(default_factory=dict)
    actor: str = "human:web"
    idempotency_key: str | None = None


class EvidenceCreate(BaseModel):
    source_type: Literal["web", "personal", "model", "report", "interview", "other"] = "web"
    url: str | None = None
    title: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)
    published_at: str | None = None
    excerpt: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class ClaimCreate(BaseModel):
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    value: Any
    valid_from: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"] = "high"
    temporal_basis: Literal["observed", "source", "unknown"] = "observed"
    actor: str = "human:web"
    idempotency_key: str | None = None


class VisibilityCreate(BaseModel):
    reason: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class AssessmentCreate(VisibilityCreate):
    status: Literal["accepted", "disputed", "invalid", "superseded"]


class ReviewCreate(VisibilityCreate):
    decision: Literal["accepted", "rejected"] = "accepted"


class ValidityEndCreate(VisibilityCreate):
    valid_to: str = Field(min_length=1)


class ChallengeCreate(BaseModel):
    problem: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    example_entity: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    proposed_replacement: dict[str, Any] | None = None
    actor: str = "human:web"
    idempotency_key: str | None = None


class ChallengeResolveCreate(BaseModel):
    status: Literal["resolved", "dismissed"]
    resolution: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class NotFoundCreate(BaseModel):
    subject: str = Field(min_length=1)
    question: str = Field(min_length=1)
    query: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    actor: str = "human:web"
    idempotency_key: str | None = None


class WorkspaceAgentCreate(BaseModel):
    message: str = Field(min_length=1)
    auto_approve: bool = False


class CuratorCreate(BaseModel):
    objective: str = Field(min_length=1)
    budget_usd: float = Field(gt=0, le=10000)
    max_cycles: int = Field(default=10, ge=1, le=50)
    max_research_cells: int = Field(default=150, ge=0, le=5000)


class CuratorBudgetAdd(BaseModel):
    amount_usd: float = Field(gt=0, le=10000)
    direction: str | None = Field(default=None, min_length=1)


def create_ledger_app(
    project: str | Path | None = None,
    frontend: str | Path | None = None,
    workspace_agent_runner: WorkspaceAgentRunner | None = None,
    research_runner: ResearchRunner | None = None,
) -> FastAPI:
    """Create the ledger-only local web API."""
    configured_root = Path(
        project or os.environ.get("EPIQ_PROJECT", ".epiq/project")
    ).resolve()
    projects_directory = Path(
        os.environ.get("EPIQ_PROJECTS_DIR", str(configured_root.parent))
    ).resolve()
    active_project_marker = projects_directory / ".epiq-active-project"
    root = configured_root
    if active_project_marker.is_file():
        remembered = Path(active_project_marker.read_text().strip()).resolve()
        if remembered.is_relative_to(projects_directory) and (remembered / "epiq.toml").is_file():
            root = remembered
    checkout_frontend = Path(__file__).parents[2] / "web" / "dist"
    packaged_frontend = Path(__file__).parent / "_frontend"
    frontend_path = Path(
        frontend or (packaged_frontend if packaged_frontend.exists() else checkout_frontend)
    ).resolve()
    app = FastAPI(title="Epiq", version="0.1.0")
    app.state.project = root
    app.state.projects_directory = projects_directory
    app.state.active_project_marker = active_project_marker
    app.state.project_open = root.exists()
    app.state.workspace_agent_runner = workspace_agent_runner or PiWorkspaceAgentRunner(root)
    app.state.workspace_agent_runner_is_default = workspace_agent_runner is None
    app.state.research_runner = research_runner or OpenAIResearchRunner()
    app.state.cli_execution = research_runner is None
    app.state.research_jobs: dict[str, dict[str, Any]] = {}
    app.state.curator: dict[str, Any] | None = None
    app.state.research_lock = threading.RLock()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(EpiqError)
    async def domain_error(_request, error: EpiqError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": error.as_dict()})

    def database() -> LedgerDatabase:
        if not root.exists():
            raise EpiqError(
                "project_not_found",
                "No ledger project has been initialized",
                "POST /api/project with a project name.",
            )
        if not app.state.project_open:
            raise EpiqError(
                "project_not_selected",
                "No project is currently open",
                "Open the configured ledger project.",
            )
        return LedgerDatabase(root)

    def project_directories() -> list[Path]:
        candidates = {root}
        directory = app.state.projects_directory
        if directory.exists():
            candidates.update(
                path.parent for path in directory.glob("*/epiq.toml") if path.is_file()
            )
        return sorted(candidates)

    def select_project(path: Path) -> LedgerDatabase:
        nonlocal root
        selected = LedgerDatabase(path)
        root = path.resolve()
        app.state.project = root
        app.state.project_open = True
        app.state.projects_directory.mkdir(parents=True, exist_ok=True)
        marker = app.state.active_project_marker
        temporary_marker = marker.with_suffix(".tmp")
        temporary_marker.write_text(f"{root}\n")
        temporary_marker.replace(marker)
        if app.state.workspace_agent_runner_is_default:
            app.state.workspace_agent_runner = PiWorkspaceAgentRunner(root)
        with app.state.research_lock:
            app.state.research_jobs.clear()
        hydrate_workspace_history()
        load_curator()
        return selected

    def project_slug(name: str) -> str:
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        slug = "-".join(part for part in re.split(r"[^a-zA-Z0-9]+", ascii_name.lower()) if part)
        return slug or "epiq-project"

    def ensure_project_switch_safe() -> None:
        with app.state.research_lock:
            active = any(
                job.get("status") in {"queued", "running"}
                or job.get("approval_status") == "applying"
                for job in app.state.research_jobs.values()
            )
            active = active or bool(
                app.state.curator
                and app.state.curator.get("status")
                in {"planning", "awaiting_approval", "running", "stopping"}
            )
        if active:
            raise EpiqError(
                "research_active",
                "Wait for active Control Center work before switching projects",
            )

    def now() -> str:
        return datetime.now(UTC).isoformat()

    def curator_cost_rates() -> tuple[float, float]:
        return (
            float(os.environ.get("EPIQ_CURATOR_PLANNING_COST_USD", "0.75")),
            float(os.environ.get("EPIQ_CURATOR_RESEARCH_CELL_COST_USD", "0.25")),
        )

    def curator_path() -> Path:
        return root / ".epiq" / "curator.json"

    def save_curator() -> None:
        state = app.state.curator
        if state is None or not root.exists():
            return
        path = curator_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)

    def load_curator() -> None:
        path = curator_path()
        if not path.is_file():
            app.state.curator = None
            return
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            app.state.curator = None
            return
        if state.get("status") in {"planning", "running", "stopping"}:
            state["status"] = "paused"
            state["message"] = "Paused after server restart; resume when ready."
        state.setdefault("budget_usd", None)
        state.setdefault("estimated_spend_usd", 0.0)
        state.setdefault("reserved_usd", 0.0)
        state.setdefault("cost_basis", "estimated")
        if state.get("budget_usd") is None and state.get("status") == "paused":
            state.update(
                status="stopped",
                message=(
                    "This earlier curator had no cash budget; create a budgeted curation "
                    "plan to continue."
                ),
                finished_at=now(),
            )
        app.state.curator = state
        save_curator()

    def workspace_context(current: LedgerDatabase) -> dict[str, Any]:
        def table_context(kind: str) -> dict[str, Any]:
            matrix = current.matrix(kind)
            not_found = {
                (item["subject_id"], item["question_id"])
                for item in current.research_outcomes(kind=kind)
            }
            questions = []
            for question in matrix["questions"]:
                states = {"answered": 0, "not_found": 0, "unasked": 0}
                current_answers = []
                for entity in matrix["entities"]:
                    claims = matrix["cells"].get(entity["entity_id"], {}).get(
                        question["question_id"], []
                    )
                    if claims:
                        states["answered"] += 1
                        current_answers.extend(
                            {
                                "row": entity["name"],
                                "claim_id": claim["claim_id"],
                                "value": claim["value"],
                            }
                            for claim in claims
                        )
                    else:
                        outcome = (entity["entity_id"], question["question_id"]) in not_found
                        states["not_found" if outcome else "unasked"] += 1
                questions.append(
                    {
                        "name": question["name"],
                        "value_type": question["value_type"],
                        "definition": question["definition"],
                        "coverage": states,
                        "current_answers": current_answers,
                    }
                )
            return {
                "kind": kind,
                "rows": [row["name"] for row in matrix["entities"]],
                "questions": questions,
            }

        return {
            "project": current.project(),
            "storage": "git-ledger",
            "tables": [table_context(kind) for kind in current.kinds()],
        }

    def update_job(job_id: str, **changes: Any) -> None:
        with app.state.research_lock:
            app.state.research_jobs[job_id].update(changes)

    def plan_commands(plan: dict[str, Any]) -> list[dict[str, Any]]:
        commands: list[dict[str, Any]] = []
        claims_by_id = {
            claim["claim_id"]: claim for claim in database().claims(include_closed=True)
        }
        kinds = dict.fromkeys(
            [
                *plan["entity_kinds"],
                *[item["kind"] for item in plan["entities"]],
                *[item["kind"] for item in plan["questions"]],
            ]
        )
        for kind in kinds:
            commands.append(
                {"argv": ["epiq", "--project", str(root), "kind", kind], "status": "proposed"}
            )
        for item in plan["entities"]:
            commands.append(
                {
                    "argv": ["epiq", "--project", str(root), "entity", item["kind"], item["name"]],
                    "status": "proposed",
                }
            )
        for item in plan["questions"]:
            definition = {
                "label": item.get("label") or item["name"].replace("_", " ").title(),
                "cardinality": item.get("cardinality", "one"),
                "volatility": item.get("volatility", "stable"),
                "freshness_days": item.get("freshness_days"),
                "research_guidance": item.get("research_guidance", ""),
            }
            commands.append(
                {
                    "argv": [
                        "epiq",
                        "--project",
                        str(root),
                        "question",
                        item["name"],
                        "--for",
                        item["kind"],
                        "--type",
                        item["value_type"],
                        "--definition",
                        json.dumps(definition, sort_keys=True),
                    ],
                    "status": "proposed",
                }
            )
        for item in plan.get("retractions", []):
            commands.append(
                {
                    "argv": [
                        "epiq",
                        "--project",
                        str(root),
                        "retract-claim",
                        item["claim_id"],
                        "--reason",
                        item["reason"],
                    ],
                    "status": "proposed",
                }
            )
        for item in plan.get("replacements", []):
            claim = claims_by_id.get(item["claim_id"])
            if claim is None:
                raise EpiqError(
                    "claim_not_found",
                    f"Replacement claim not found: {item['claim_id']}",
                )
            argv = [
                "epiq",
                "--project",
                str(root),
                "supersede-claim",
                item["claim_id"],
                "--value",
                json.dumps(item["value"]),
                "--valid-from",
                claim["valid_from"],
            ]
            for evidence_id in claim["evidence_ids"]:
                argv.extend(["--evidence-id", evidence_id])
            argv.extend(
                [
                    "--reason",
                    item["reason"],
                    "--confidence",
                    claim["confidence"],
                    "--temporal-basis",
                    claim["temporal_basis"],
                ]
            )
            commands.append({"argv": argv, "status": "proposed"})
        for item in plan["research"]:
            argv = [
                "epiq",
                "--project",
                str(root),
                "research",
                "--kind",
                item["kind"],
                "--question",
                item["question"],
            ]
            for name in item.get("entity_names", []):
                argv.extend(["--entity-name", name])
            if item.get("instructions"):
                argv.extend(["--instructions", str(item["instructions"])])
            if item.get("allow_estimates"):
                argv.append("--allow-estimates")
            commands.append({"argv": argv, "status": "proposed"})
        return commands

    def execute_cli(argv: list[str]) -> dict[str, Any]:
        """Execute one fixed argv command without a shell and return its JSON envelope."""
        if argv[:3] != ["epiq", "--project", str(root)]:
            raise EpiqError("unsafe_command", "Control Center command targets another project")
        completed = subprocess.run(
            [sys.executable, "-m", "epiq", *argv[1:]],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        payload_text = completed.stdout.strip() or completed.stderr.strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as error:
            raise EpiqError(
                "invalid_command_result", f"Epiq returned non-JSON output: {payload_text[-500:]}"
            ) from error
        if completed.returncode or payload.get("status") != "ok":
            raise EpiqError(
                "command_failed",
                str(payload.get("errors") or f"Command exited {completed.returncode}"),
            )
        return payload

    def hydrate_workspace_history() -> None:
        if not root.exists() or not app.state.project_open:
            return
        current = LedgerDatabase(root)
        for proposal in current.proposals():
            if proposal.get("proposal_type") != "workspace_agent":
                continue
            document = proposal.get("document", {})
            plan = document.get("workspace_plan")
            message = document.get("user_message")
            if not isinstance(plan, dict) or not isinstance(message, str):
                continue
            reviews = proposal.get("reviews", [])
            review = reviews[-1] if reviews else None
            decision = review.get("decision") if isinstance(review, dict) else None
            approval = (
                "approved"
                if decision == "accepted"
                else "rejected"
                if decision == "rejected"
                else "pending"
            )
            job_id = f"history_{proposal['proposal_id']}"
            estimated = sum(len(item.get("entity_names", [])) for item in plan["research"])
            app.state.research_jobs[job_id] = {
                "job_id": job_id,
                "job_type": "workspace_agent",
                "entity_kind": "Workspace",
                "question_id": "",
                "mode": "workspace_agent",
                "instructions": message,
                "user_message": message,
                "status": "completed",
                "total": estimated,
                "completed": estimated if approval == "approved" else 0,
                "target_entity_ids": [],
                "error": None,
                "messages": [{"at": "", "message": "Loaded from the project ledger"}],
                "outcome": "workspace_proposal" if approval == "pending" else "changed",
                "assistant_summary": str(plan.get("summary", "")).strip(),
                "workspace_plan": plan,
                "approval_status": approval,
                "estimated_research_cells": estimated,
                "proposal_id": proposal["proposal_id"],
                "command_transcript": plan_commands(plan),
                "historical": True,
            }

    hydrate_workspace_history()
    load_curator()

    def plan_workspace(job_id: str, message: str) -> None:
        def progress(text: str) -> None:
            with app.state.research_lock:
                app.state.research_jobs[job_id]["messages"].append({"at": now(), "message": text})

        update_job(job_id, status="running", started_at=now())
        try:
            current = database()
            progress("Thinking through your request")
            context = workspace_context(current)
            plan = app.state.workspace_agent_runner(message, context, progress)
            entities = list(plan["entities"])[:15]
            proposed = {
                (str(item["kind"]), str(item["name"]).casefold()) for item in entities
            }
            existing = {
                (str(table["kind"]), str(name).casefold())
                for table in context["tables"]
                for name in table["rows"]
            }
            new_kinds = {
                str(kind) for kind in plan["entity_kinds"] if str(kind) not in current.kinds()
            }
            # A bounded plan must remain executable after bounding. Preserve rows referenced by
            # research on a newly proposed table even when they fall beyond the display-oriented
            # seed-row limit.
            for request in plan["research"]:
                kind = str(request["kind"])
                for name in request.get("entity_names", []):
                    key = (kind, str(name).casefold())
                    if key not in proposed and key not in existing and kind in new_kinds:
                        entities.append({"kind": kind, "name": str(name)})
                        proposed.add(key)
            plan = {
                **plan,
                "entity_kinds": list(dict.fromkeys(str(x) for x in plan["entity_kinds"]))[:6],
                "entities": entities,
                "questions": list(plan["questions"])[:24],
                "retractions": list(plan.get("retractions", []))[:24],
                "replacements": list(plan.get("replacements", []))[:24],
                "research": list(plan["research"])[:24],
            }
            with app.state.research_lock:
                research_cell_limit = app.state.research_jobs[job_id].get(
                    "research_cell_limit"
                )
            if research_cell_limit is not None:
                remaining = max(0, int(research_cell_limit))
                bounded_research = []
                for request in plan["research"]:
                    names = list(request.get("entity_names", []))[:remaining]
                    if names:
                        bounded_research.append({**request, "entity_names": names})
                        remaining -= len(names)
                    if remaining == 0:
                        break
                plan = {**plan, "research": bounded_research}
            estimated = sum(len(item.get("entity_names", [])) for item in plan["research"])
            proposal = current.create_proposal(
                {"user_message": message, "workspace_plan": plan},
                proposal_type="workspace_agent",
                actor="agent:web",
                idempotency_key=job_id,
            )
            update_job(
                job_id,
                status="completed",
                outcome="workspace_proposal",
                assistant_summary=str(plan["summary"]).strip(),
                workspace_plan=plan,
                approval_status="pending",
                estimated_research_cells=estimated,
                proposal_id=proposal["proposal_id"],
                command_transcript=plan_commands(plan),
                finished_at=now(),
            )
            with app.state.research_lock:
                auto_approve = bool(app.state.research_jobs[job_id].get("auto_approve"))
                if auto_approve:
                    app.state.research_jobs[job_id]["approval_status"] = "applying"
            if auto_approve:
                progress("YOLO mode enabled · applying the proposed CLI plan automatically")
                apply_workspace_plan(job_id)
        except Exception as error:
            update_job(job_id, status="failed", error=str(error), finished_at=now())

    def apply_workspace_plan(job_id: str) -> None:
        with app.state.research_lock:
            job = app.state.research_jobs[job_id]
            plan = job.get("workspace_plan")
        if not plan:
            raise EpiqError("workspace_plan_unavailable", "This workspace plan is unavailable")
        current = database()
        existing_kinds = set(current.kinds())
        existing_entities = {(item["kind"], item["name"].casefold()) for item in current.entities()}
        existing_questions = {(item["subject_kind"], item["name"]) for item in current.questions()}
        written = 0
        command_results: list[dict[str, Any]] = []

        def record_result(argv: list[str], data: Any) -> None:
            command_results.append(
                {
                    "argv": argv,
                    "result": data
                    if isinstance(data, dict) and data.get("schema_version") == "1.0"
                    else {
                        "schema_version": "1.0",
                        "status": "ok",
                        "command": " ".join(argv[:4]),
                        "argv": argv,
                        "data": data,
                        "errors": [],
                        "warnings": [],
                        "next_steps": [],
                    },
                }
            )

        for kind in dict.fromkeys(
            [
                *plan["entity_kinds"],
                *[item["kind"] for item in plan["entities"]],
                *[item["kind"] for item in plan["questions"]],
            ]
        ):
            if kind not in existing_kinds:
                argv = ["epiq", "--project", str(root), "kind", kind]
                result = (
                    execute_cli(argv)
                    if app.state.cli_execution
                    else current.define_kind(
                        kind, actor="agent:web", idempotency_key=f"{job_id}:kind:{kind}"
                    )
                )
                record_result(argv, result)
                existing_kinds.add(kind)
                written += 1
        for item in plan["entities"]:
            key = (item["kind"], item["name"].casefold())
            if key not in existing_entities:
                argv = ["epiq", "--project", str(root), "entity", item["kind"], item["name"]]
                result = (
                    execute_cli(argv)
                    if app.state.cli_execution
                    else current.create_entity(
                        item["kind"],
                        item["name"],
                        actor="agent:web",
                        idempotency_key=f"{job_id}:entity:{item['kind']}:{item['name']}",
                    )
                )
                record_result(argv, result)
                existing_entities.add(key)
                written += 1
        for item in plan["questions"]:
            key = (item["kind"], item["name"])
            if key not in existing_questions:
                definition = {
                    "label": item.get("label") or item["name"].replace("_", " ").title(),
                    "cardinality": item.get("cardinality", "one"),
                    "volatility": item.get("volatility", "stable"),
                    "freshness_days": item.get("freshness_days"),
                    "research_guidance": item.get("research_guidance", ""),
                }
                argv = [
                    "epiq",
                    "--project",
                    str(root),
                    "question",
                    item["name"],
                    "--for",
                    item["kind"],
                    "--type",
                    item["value_type"],
                    "--definition",
                    json.dumps(definition, sort_keys=True),
                ]
                result = (
                    execute_cli(argv)
                    if app.state.cli_execution
                    else current.define_question(
                        item["name"],
                        item["kind"],
                        item["value_type"],
                        definition=definition,
                        actor="agent:web",
                        idempotency_key=f"{job_id}:question:{item['kind']}:{item['name']}",
                    )
                )
                record_result(argv, result)
                existing_questions.add(key)
                written += 1
        for item in plan.get("retractions", []):
            argv = [
                "epiq",
                "--project",
                str(root),
                "retract-claim",
                item["claim_id"],
                "--reason",
                item["reason"],
            ]
            result = (
                execute_cli(argv)
                if app.state.cli_execution
                else current.retract_claim(
                    item["claim_id"],
                    item["reason"],
                    actor="agent:web",
                    idempotency_key=f"{job_id}:retract:{item['claim_id']}",
                )
            )
            record_result(argv, result)
            written += 1
        claims_by_id = {
            claim["claim_id"]: claim for claim in current.claims(include_closed=True)
        }
        for item in plan.get("replacements", []):
            claim = claims_by_id.get(item["claim_id"])
            if claim is None:
                raise EpiqError(
                    "claim_not_found",
                    f"Replacement claim not found: {item['claim_id']}",
                )
            argv = [
                "epiq",
                "--project",
                str(root),
                "supersede-claim",
                item["claim_id"],
                "--value",
                json.dumps(item["value"]),
                "--valid-from",
                claim["valid_from"],
            ]
            for evidence_id in claim["evidence_ids"]:
                argv.extend(["--evidence-id", evidence_id])
            argv.extend(
                [
                    "--reason",
                    item["reason"],
                    "--confidence",
                    claim["confidence"],
                    "--temporal-basis",
                    claim["temporal_basis"],
                ]
            )
            result = (
                execute_cli(argv)
                if app.state.cli_execution
                else current.supersede_claim(
                    item["claim_id"],
                    item["value"],
                    valid_from=claim["valid_from"],
                    evidence_ids=claim["evidence_ids"],
                    reason=item["reason"],
                    confidence=claim["confidence"],
                    temporal_basis=claim["temporal_basis"],
                    actor="agent:web",
                    idempotency_key=f"{job_id}:replace:{item['claim_id']}",
                )
            )
            record_result(argv, result)
            written += 1
        current.review_proposal(
            job["proposal_id"],
            "accepted",
            "Approved in the workspace agent console",
            actor="human:web",
            idempotency_key=f"{job_id}:approve",
        )
        messages = [
            *job["messages"],
            {
                "at": now(),
                "message": f"Applied {written} schema and row changes.",
            },
        ]
        if plan["research"]:
            update_job(
                job_id,
                status="queued",
                outcome=None,
                approval_status="approved",
                written=written,
                command_results=command_results,
                messages=messages,
            )
            threading.Thread(
                target=execute_workspace_research,
                args=(job_id,),
                daemon=True,
                name=f"epiq-research-{job_id}",
            ).start()
        else:
            update_job(
                job_id,
                status="completed",
                outcome="changed",
                approval_status="approved",
                written=written,
                command_results=command_results,
                finished_at=now(),
                messages=messages,
            )

    def execute_workspace_research(job_id: str) -> None:
        def progress(text: str) -> None:
            with app.state.research_lock:
                app.state.research_jobs[job_id]["messages"].append({"at": now(), "message": text})

        update_job(job_id, status="running", started_at=now())
        try:
            current = database()
            with app.state.research_lock:
                plan = app.state.research_jobs[job_id]["workspace_plan"]
                schema_writes = int(app.state.research_jobs[job_id].get("written", 0))
                research_commands = [
                    item["argv"]
                    for item in app.state.research_jobs[job_id].get("command_transcript", [])
                    if item["argv"][3:4] == ["research"]
                ]
            if app.state.cli_execution:
                estimated_targets = sum(
                    len(item.get("entity_names", [])) for item in plan["research"]
                )
                update_job(
                    job_id,
                    total=estimated_targets or len(research_commands),
                    completed=0,
                )
                written = 0
                no_result = 0
                for argv in research_commands:
                    with app.state.research_lock:
                        if app.state.research_jobs[job_id].get("cancel_requested"):
                            update_job(job_id, status="cancelled", finished_at=now())
                            return
                    question_name = (
                        argv[argv.index("--question") + 1]
                        if "--question" in argv
                        else "field"
                    )
                    named_targets = argv.count("--entity-name")
                    progress(
                        f"Researching {question_name}"
                        + (f" across {named_targets} rows" if named_targets else "")
                    )
                    result = execute_cli(argv)
                    data = result.get("data", {})
                    writes = data.get("writes", []) if isinstance(data, dict) else []
                    target_count = int(data.get("target_count", len(writes)))
                    written += sum(item.get("status") == "answered" for item in writes)
                    no_result += sum(item.get("status") == "not_found" for item in writes)
                    with app.state.research_lock:
                        app.state.research_jobs[job_id].setdefault("command_results", []).append(
                            {"argv": argv, "result": result}
                        )
                        app.state.research_jobs[job_id]["completed"] += target_count
                        app.state.research_jobs[job_id]["total"] = max(
                            app.state.research_jobs[job_id]["total"],
                            app.state.research_jobs[job_id]["completed"],
                        )
                    progress(
                        f"{data.get('kind', 'Research')} · {data.get('question', 'field')}: "
                        f"processed {target_count} rows, wrote {len(writes)} results"
                    )
                progress(f"Research finished: {written} answers, {no_result} without a result")
                update_job(
                    job_id,
                    status="completed",
                    outcome="changed" if schema_writes + written else "no_change",
                    written=schema_writes + written,
                    no_result=no_result,
                    finished_at=now(),
                )
                return
            written = 0
            no_result = 0
            total = sum(len(item.get("entity_names", [])) for item in plan["research"])
            update_job(job_id, total=total, completed=0)
            for request in plan["research"]:
                request_written = 0
                request_no_result = 0
                with app.state.research_lock:
                    if app.state.research_jobs[job_id].get("cancel_requested"):
                        update_job(job_id, status="cancelled", finished_at=now())
                        return
                projection = current.matrix(str(request["kind"]))
                question = next(
                    (
                        item
                        for item in projection["questions"]
                        if item["name"] == str(request["question"])
                    ),
                    None,
                )
                if question is None:
                    progress(f"Skipped missing field {request['kind']}.{request['question']}")
                    continue
                if str(question["value_type"]).startswith("Ref["):
                    field = f"{request['kind']}.{request['question']}"
                    progress(
                        f"Skipped relationship research for {field}; "
                        "relationship proposals are not yet supported by the ledger console"
                    )
                    continue
                requested = {str(name).casefold() for name in request.get("entity_names", [])}
                targets = [
                    {
                        "entity_id": row["entity_id"],
                        "name": row["name"],
                        "attributes": row.get("attributes", {}),
                    }
                    for row in projection["entities"]
                    if not requested or str(row["name"]).casefold() in requested
                ]
                research_question = {
                    **question,
                    "task_mode": "fill_missing",
                    "instructions": str(request.get("instructions", "")),
                    "allow_estimates": bool(request.get("allow_estimates", False)),
                }
                findings = app.state.research_runner(
                    str(request["kind"]), research_question, targets, progress
                )
                targets_by_id = {item["entity_id"]: item for item in targets}
                for finding in findings:
                    with app.state.research_lock:
                        if app.state.research_jobs[job_id].get("cancel_requested"):
                            update_job(job_id, status="cancelled", finished_at=now())
                            return
                    target = targets_by_id.get(str(finding.get("entity_id")))
                    if target is None:
                        continue
                    if finding.get("status") == "not_found":
                        current.record_not_found(
                            target["entity_id"],
                            question["question_id"],
                            f"Agent research for {question['name']}",
                            str(finding.get("notes") or "No sufficient evidence found."),
                            actor="agent:web",
                        )
                        no_result += 1
                        request_no_result += 1
                    elif finding.get("status") == "answered":
                        observed_at = research_instant(finding.get("observed_as_of"))
                        published_at = research_instant(finding.get("source_published_at"))
                        current.record(
                            target["entity_id"],
                            question["question_id"],
                            finding["value"],
                            valid_from=(
                                observed_at
                                or published_at
                                or datetime.now(UTC).date().isoformat()
                            ),
                            source_title=str(finding["source_title"]),
                            source_type=str(finding.get("source_type") or "web"),
                            url=str(finding["source_url"]),
                            retrieved_at=datetime.now(UTC).date().isoformat(),
                            excerpt=str(finding["excerpt"]),
                            published_at=published_at,
                            confidence=str(finding.get("confidence") or "medium"),
                            temporal_basis=(
                                "observed"
                                if finding.get("observed_as_of")
                                else "source"
                                if finding.get("source_published_at")
                                else "unknown"
                            ),
                            epistemic_status=str(
                                finding.get("epistemic_status") or "reported"
                            ),
                            estimate=(
                                finding.get("estimate")
                                if isinstance(finding.get("estimate"), dict)
                                else {}
                            ),
                            actor="agent:web",
                        )
                        written += 1
                        request_written += 1
                    with app.state.research_lock:
                        app.state.research_jobs[job_id]["completed"] += 1
                argv = [
                    "epiq",
                    "--project",
                    str(root),
                    "research",
                    "--kind",
                    str(request["kind"]),
                    "--question",
                    str(request["question"]),
                ]
                for name in request.get("entity_names", []):
                    argv.extend(["--entity-name", str(name)])
                if request.get("instructions"):
                    argv.extend(["--instructions", str(request["instructions"])])
                if request.get("allow_estimates"):
                    argv.append("--allow-estimates")
                with app.state.research_lock:
                    app.state.research_jobs[job_id].setdefault("command_results", []).append(
                        {
                            "argv": argv,
                            "result": {
                                "schema_version": "1.0",
                                "status": "ok",
                                "command": "epiq ledger research",
                                "argv": argv,
                                "data": {
                                    "written": request_written,
                                    "not_found": request_no_result,
                                },
                                "errors": [],
                                "warnings": [],
                                "next_steps": [],
                            },
                        }
                    )
            progress(f"Research finished: {written} answers, {no_result} without a result")
            update_job(
                job_id,
                status="completed",
                outcome="changed" if written or schema_writes else "no_change",
                written=schema_writes + written,
                no_result=no_result,
                finished_at=now(),
            )
        except Exception as error:
            update_job(job_id, status="failed", error=str(error), finished_at=now())

    def run_curator(curator_id: str) -> None:
        while True:
            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                if state.get("stop_requested"):
                    state.update(
                        status="stopped",
                        message="Stopped by the user.",
                        finished_at=now(),
                    )
                    save_curator()
                    return
                if state.get("pause_requested"):
                    state.update(
                        status="paused",
                        message="Paused between enrichment cycles.",
                    )
                    save_curator()
                    return
                cycle = int(state.get("completed_cycles", 0)) + 1
                remaining_cell_cap = int(state["max_research_cells"]) - int(
                    state.get("research_cells_used", 0)
                )
                planning_cost, cell_cost = curator_cost_rates()
                budget = float(state["budget_usd"])
                remaining_usd = budget - float(state.get("estimated_spend_usd", 0.0))
                affordable_cells = max(
                    0,
                    int((remaining_usd - planning_cost) // cell_cost),
                )
                remaining_cells = min(remaining_cell_cap, affordable_cells)
                if (
                    cycle > int(state["max_cycles"])
                    or remaining_cell_cap <= 0
                    or remaining_usd < planning_cost
                ):
                    state.update(
                        status="completed",
                        message="Finished the configured cash budget or safety limits.",
                        finished_at=now(),
                    )
                    save_curator()
                    return
                previous = state.get("cycles", [])[-1] if state.get("cycles") else None
                objective = str(state["objective"])
                strategy = state.get("strategy") or {}
                continuation_direction = str(
                    state.get("continuation_direction") or ""
                ).strip()
                message = (
                    f"Curator cycle {cycle}. Keep enriching this project toward this objective: "
                    f"{objective}\n\nApproved curation strategy: "
                    f"{json.dumps(strategy, sort_keys=True)}\n\n"
                    "Reflect on the current authoritative workspace, identify the "
                    "single "
                    "highest-value next enrichment, and execute a bounded step. Prefer useful "
                    "coverage, natural related tables, stronger evidence, and comparison fields. "
                    "Do not repeat completed work or add low-value fields merely to keep going. "
                    "When the objective is a landscape, roster, catalog, or collection, breadth "
                    "comes first: until the primary table has at least 25 credible examples, "
                    "normally add 5–10 new in-scope rows per cycle and research the minimum fields "
                    "needed to verify their identity and relevance. Use later cycles for deeper "
                    "comparison fields and related evidence. Favor sortable numerical values with "
                    "units, dates, mutually exclusive categories, narrowly defined yes/no fields, "
                    "URLs, and relationships over long prose. Split compound concepts into atomic "
                    "typed fields; add a String field only when the fact is genuinely textual and "
                    "cannot be represented usefully by a more structured type. If the last tactic "
                    "stalled, try a "
                    "different discovery strategy rather than immediately stopping."
                )
                if continuation_direction:
                    message += (
                        "\n\nThe user added this direction when extending the budget: "
                        f"{continuation_direction}. Treat it as the priority within the approved "
                        "strategy for this continuation."
                    )
                if previous:
                    message += (
                        "\n\nPrevious cycle outcome: "
                        f"{previous.get('summary', '')} "
                        f"({previous.get('written', 0)} writes; status "
                        f"{previous.get('status', 'unknown')}). Choose a genuinely useful "
                        "next step."
                    )
                job_id = f"curator_{curator_id}_{cycle}"
                job = {
                    "job_id": job_id,
                    "job_type": "workspace_agent",
                    "entity_kind": "Workspace",
                    "question_id": "",
                    "mode": "workspace_agent",
                    "instructions": message,
                    "user_message": f"Curator cycle {cycle}: {objective}",
                    "auto_approve": True,
                    "curator_id": curator_id,
                    "curator_cycle": cycle,
                    "research_cell_limit": remaining_cells,
                    "estimated_planning_cost_usd": planning_cost,
                    "estimated_research_cell_cost_usd": cell_cost,
                    "status": "queued",
                    "total": 0,
                    "completed": 0,
                    "target_entity_ids": [],
                    "created_at": now(),
                    "error": None,
                    "messages": [{"at": now(), "message": f"Curator cycle {cycle} queued"}],
                }
                app.state.research_jobs[job_id] = job
                state.update(
                    status="running",
                    active_job_id=job_id,
                    reserved_usd=round(planning_cost, 4),
                    message=f"Cycle {cycle}: reflecting on the next enrichment.",
                )
                save_curator()

            plan_workspace(job_id, message)
            while True:
                with app.state.research_lock:
                    job = app.state.research_jobs[job_id]
                    terminal = job.get("status") in {"completed", "failed", "cancelled"}
                    applying = job.get("approval_status") == "applying"
                if terminal and not applying:
                    break
                time.sleep(0.1)

            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                job = app.state.research_jobs[job_id]
                attempted = int(job.get("estimated_research_cells", 0))
                cycle_cost = planning_cost + attempted * cell_cost
                cycle_record = {
                    "cycle": cycle,
                    "job_id": job_id,
                    "status": job.get("status"),
                    "summary": job.get("assistant_summary") or job.get("error") or "",
                    "written": int(job.get("written", 0)),
                    "research_cells": attempted,
                    "estimated_cost_usd": round(cycle_cost, 4),
                    "finished_at": now(),
                }
                state.setdefault("cycles", []).append(cycle_record)
                state["completed_cycles"] = cycle
                state["research_cells_used"] = int(
                    state.get("research_cells_used", 0)
                ) + attempted
                state["estimated_spend_usd"] = round(
                    min(
                        float(state["budget_usd"]),
                        float(state.get("estimated_spend_usd", 0.0)) + cycle_cost,
                    ),
                    4,
                )
                state["no_change_streak"] = (
                    int(state.get("no_change_streak", 0)) + 1
                    if int(job.get("written", 0)) == 0
                    else 0
                )
                state["active_job_id"] = None
                state["reserved_usd"] = 0.0
                state["message"] = str(cycle_record["summary"])
                if job.get("status") == "failed":
                    state.update(
                        status="paused",
                        message=f"Paused after cycle {cycle} failed: {job.get('error')}",
                    )
                    save_curator()
                    return
                if int(state["no_change_streak"]) >= 2:
                    state.update(
                        status="completed",
                        message="Stopped after two different cycles produced no useful changes.",
                        finished_at=now(),
                    )
                    save_curator()
                    return
                save_curator()

    def start_curator_thread(curator_id: str) -> None:
        threading.Thread(
            target=run_curator,
            args=(curator_id,),
            daemon=True,
            name=f"epiq-curator-{curator_id}",
        ).start()

    def plan_curator_strategy(curator_id: str) -> None:
        def progress(text: str) -> None:
            with app.state.research_lock:
                state = app.state.curator
                if state and state.get("curator_id") == curator_id:
                    state["message"] = text
                    save_curator()

        try:
            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                objective = str(state["objective"])
                budget_usd = float(state["budget_usd"])
            current = database()
            context = workspace_context(current)
            strategy_method = getattr(
                app.state.workspace_agent_runner, "curator_strategy", None
            )
            if callable(strategy_method):
                strategy = strategy_method(objective, context, budget_usd, progress)
            else:
                primary_kind = (
                    str(context["tables"][0]["kind"])
                    if context.get("tables")
                    else "PrimaryEntity"
                )
                strategy = {
                    "summary": f"Build broad, comparable coverage for {objective}",
                    "primary_entity_kind": primary_kind,
                    "target_example_count": 25,
                    "priority_fields": [],
                    "phases": [
                        {
                            "name": "Breadth",
                            "objective": "Discover and verify in-scope examples.",
                            "target_new_rows": 25,
                            "estimated_research_cells": 0,
                            "budget_usd": max(0.0, budget_usd - curator_cost_rates()[0]),
                        }
                    ],
                    "stopping_criteria": [
                        "Cash budget exhausted",
                        "Two consecutive cycles make no useful changes",
                    ],
                }
            planning_cost, _ = curator_cost_rates()
            with app.state.research_lock:
                state = app.state.curator
                if state is None or state.get("curator_id") != curator_id:
                    return
                if state.get("stop_requested"):
                    state.update(
                        status="stopped",
                        reserved_usd=0.0,
                        message="Stopped by the user.",
                        finished_at=now(),
                    )
                    save_curator()
                    return
                state.update(
                    status="awaiting_approval",
                    strategy=strategy,
                    estimated_spend_usd=round(planning_cost, 4),
                    reserved_usd=0.0,
                    message="Review the curation strategy before spending the remaining budget.",
                )
                save_curator()
        except Exception as error:
            with app.state.research_lock:
                state = app.state.curator
                if state and state.get("curator_id") == curator_id:
                    state.update(
                        status="paused",
                        reserved_usd=0.0,
                        message=f"Strategy planning failed: {error}",
                    )
                    save_curator()

    def start_curator_strategy_thread(curator_id: str) -> None:
        threading.Thread(
            target=plan_curator_strategy,
            args=(curator_id,),
            daemon=True,
            name=f"epiq-curator-strategy-{curator_id}",
        ).start()

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "project": (
                "ready"
                if root.exists() and app.state.project_open
                else "closed"
                if root.exists()
                else "uninitialized"
            ),
            "path": str(root),
            "initialized": root.exists(),
            "storage": "git-ledger",
        }

    @app.post("/api/project", status_code=201)
    def initialize_project(body: ProjectCreate) -> dict[str, Any]:
        created = LedgerDatabase(root, create=True, name=body.name, actor=body.actor)
        app.state.project_open = True
        return {"project": created.project(), "path": str(root)}

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        ensure_project_switch_safe()
        directory = app.state.projects_directory
        directory.mkdir(parents=True, exist_ok=True)
        slug = project_slug(body.name)
        path = directory / slug
        suffix = 2
        while path.exists():
            path = directory / f"{slug}-{suffix}"
            suffix += 1
        created = LedgerDatabase(path, create=True, name=body.name, actor=body.actor)
        select_project(path)
        project = created.project()
        return {
            "project_id": project["project_id"],
            "name": project["name"],
            "path": str(path),
            "active": True,
        }

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        available = []
        for path in project_directories():
            if not (path / "epiq.toml").exists():
                continue
            project = LedgerDatabase(path).project()
            available.append(
                {
                    "project_id": project["project_id"],
                    "name": project["name"],
                    "path": str(path),
                    "active": path.resolve() == root and bool(app.state.project_open),
                }
            )
        return available

    @app.post("/api/projects/open")
    def open_project(body: ProjectOpen) -> dict[str, Any]:
        selected_path = None
        selected = None
        for path in project_directories():
            if not (path / "epiq.toml").exists():
                continue
            candidate = LedgerDatabase(path)
            if candidate.project()["project_id"] == body.project_id:
                selected_path = path
                selected = candidate
                break
        if selected_path is None or selected is None:
            raise EpiqError("project_not_found", "The selected project is not configured")
        if selected_path.resolve() != root:
            ensure_project_switch_safe()
        project = select_project(selected_path).project()
        return {
            "project_id": project["project_id"],
            "name": project["name"],
            "path": str(selected_path),
            "active": True,
        }

    @app.post("/api/projects/close")
    def close_project() -> dict[str, bool]:
        with app.state.research_lock:
            active = any(
                job["status"] in {"queued", "running"} for job in app.state.research_jobs.values()
            )
        if active:
            raise EpiqError(
                "research_active", "Wait for active research before closing this project"
            )
        app.state.project_open = False
        return {"closed": True}

    @app.get("/api/project")
    def project_info() -> dict[str, Any]:
        current = database()
        entities = current.entities()
        questions = current.questions()
        return {
            "project": current.project(),
            "entity_kinds": [
                {
                    "kind": kind,
                    "entities": sum(item["kind"] == kind for item in entities),
                    "questions": sum(item["subject_kind"] == kind for item in questions),
                }
                for kind in current.kinds()
            ],
            "storage": "git-ledger",
        }

    @app.post("/api/entity-kinds", status_code=201)
    def create_kind(body: KindCreate) -> dict[str, str]:
        result = database().define_kind(
            body.kind, actor=body.actor, idempotency_key=body.idempotency_key
        )
        return {"kind": result["kind"]}

    @app.post("/api/entities", status_code=201)
    def create_entity(body: EntityCreate) -> dict[str, str]:
        result = database().create_entity(
            body.kind,
            body.name,
            attributes=body.attributes,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"entity_id": result["entity_id"]}

    @app.post("/api/entities/{entity_id}/retire")
    def retire_entity(entity_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_entity_visibility(
            entity_id,
            False,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"entity_id": entity_id, "status": "retired"}

    @app.post("/api/entities/{entity_id}/restore")
    def restore_entity(entity_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_entity_visibility(
            entity_id,
            True,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"entity_id": entity_id, "status": "active"}

    @app.post("/api/questions", status_code=201)
    def create_question(body: QuestionCreate) -> dict[str, str]:
        result = database().define_question(
            body.name,
            body.subject_kind,
            body.value_type,
            definition=body.definition,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"question_id": result["question_id"]}

    @app.post("/api/questions/{question_id}/retire")
    def retire_question(question_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_question_visibility(
            question_id,
            False,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"question_id": question_id, "status": "retired"}

    @app.post("/api/questions/{question_id}/restore")
    def restore_question(question_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().set_question_visibility(
            question_id,
            True,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"question_id": question_id, "status": "active"}

    @app.post("/api/questions/{question_id}/challenges", status_code=201)
    def challenge_question(question_id: str, body: ChallengeCreate) -> dict[str, str]:
        result = database().challenge_question(
            question_id,
            body.problem,
            body.explanation,
            example_entity_id=body.example_entity,
            evidence_ids=body.evidence_ids,
            proposed_replacement=body.proposed_replacement,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"challenge_id": result["challenge_id"], "status": "open"}

    @app.post("/api/evidence", status_code=201)
    def create_evidence(body: EvidenceCreate) -> dict[str, str]:
        if body.source_type == "web" and not body.url:
            raise EpiqError("source_url_required", "A web source requires a URL")
        result = database().capture_evidence(
            source_title=body.title,
            source_type=body.source_type,
            url=body.url or f"urn:epiq:{body.source_type}",
            retrieved_at=body.retrieved_at,
            published_at=body.published_at,
            excerpt=body.excerpt,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"source_id": result["source_id"], "evidence_id": result["evidence_id"]}

    @app.post("/api/evidence/{evidence_id}/assess", status_code=201)
    def assess_evidence(evidence_id: str, body: AssessmentCreate) -> dict[str, str]:
        result = database().assess_evidence(
            evidence_id,
            body.status,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"assessment_id": result["assessment_id"], "status": body.status}

    @app.post("/api/claims", status_code=201)
    def create_claim(body: ClaimCreate) -> dict[str, str]:
        result = database().assert_claim(
            body.subject,
            body.question,
            body.value,
            valid_from=body.valid_from,
            evidence_ids=body.evidence_ids,
            confidence=body.confidence,
            temporal_basis=body.temporal_basis,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": result["claim_id"]}

    @app.post("/api/claims/{claim_id}/review")
    def review_claim(claim_id: str, body: ReviewCreate) -> dict[str, str]:
        result = database().review_claim(
            claim_id,
            body.decision,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": claim_id, "review_id": result["review_id"]}

    @app.post("/api/claims/{claim_id}/retract")
    def retract_claim(claim_id: str, body: VisibilityCreate) -> dict[str, str]:
        database().retract_claim(
            claim_id,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": claim_id, "status": "retracted"}

    @app.post("/api/claims/{claim_id}/validity-end", status_code=201)
    def end_claim_validity(claim_id: str, body: ValidityEndCreate) -> dict[str, str]:
        database().end_claim_validity(
            claim_id,
            body.valid_to,
            body.reason,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"claim_id": claim_id, "valid_to": body.valid_to}

    @app.get("/api/claims-long")
    def claims(kind: str | None = None, question: str | None = None) -> dict[str, Any]:
        items = database().claims(kind=kind, question=question)
        return {"count": len(items), "claims": items}

    @app.get("/api/matrix/{entity_kind}")
    def matrix(entity_kind: str) -> dict[str, Any]:
        current = database()
        return _web_matrix(current.matrix(entity_kind), current)

    @app.get("/api/related/{entity_id}")
    def related(
        entity_id: str,
        via: str | None = None,
        direction: Literal["incoming", "outgoing", "both"] = "both",
        depth: int = 1,
    ) -> dict[str, Any]:
        if depth < 1 or depth > 20:
            raise EpiqError("invalid_depth", "Relationship depth must be between 1 and 20")
        current = database()
        entities = {item["entity_id"]: item for item in current.entities()}
        target = entities.get(entity_id)
        if target is None:
            raise EpiqError("missing_domain_object", f"Missing entity_id: {entity_id}")
        questions = {
            item["question_id"]: item
            for item in current.questions()
            if str(item["value_type"]).startswith("Ref[") and (via is None or item["name"] == via)
        }
        graph_edges = []
        for claim in current.claims():
            question = questions.get(claim["question_id"])
            if question is None:
                continue
            identifiers = claim["value"] if isinstance(claim["value"], list) else [claim["value"]]
            for identifier in identifiers:
                destination = entities.get(str(identifier))
                source = entities.get(claim["subject_id"])
                if source is None or destination is None:
                    continue
                graph_edges.append(
                    {
                        "question": question["name"],
                        "claim_ids": [claim["claim_id"]],
                        "from": {
                            "entity_id": source["entity_id"],
                            "name": source["name"],
                            "kind": source["kind"],
                        },
                        "to": {
                            "entity_id": destination["entity_id"],
                            "name": destination["name"],
                            "kind": destination["kind"],
                        },
                    }
                )
        edges = []
        frontier = {entity_id: []}
        visited_entities = {entity_id}
        visited_edges: set[tuple[str, str, str, str]] = set()
        for level in range(1, depth + 1):
            next_frontier: dict[str, list[str]] = {}
            for edge in graph_edges:
                candidates = []
                if direction in {"outgoing", "both"} and edge["from"]["entity_id"] in frontier:
                    candidates.append(("outgoing", edge["to"]["entity_id"]))
                if direction in {"incoming", "both"} and edge["to"]["entity_id"] in frontier:
                    candidates.append(("incoming", edge["from"]["entity_id"]))
                for edge_direction, next_id in candidates:
                    key = (
                        edge["from"]["entity_id"],
                        edge["to"]["entity_id"],
                        edge["question"],
                        edge_direction,
                    )
                    if key in visited_edges:
                        continue
                    visited_edges.add(key)
                    current_id = (
                        edge["from"]["entity_id"]
                        if edge_direction == "outgoing"
                        else edge["to"]["entity_id"]
                    )
                    path_claim_ids = [*frontier[current_id], *edge["claim_ids"]]
                    edges.append(
                        {
                            **edge,
                            "direction": edge_direction,
                            "depth": level,
                            "path_claim_ids": path_claim_ids,
                        }
                    )
                    if next_id not in visited_entities:
                        next_frontier[next_id] = path_claim_ids
                        visited_entities.add(next_id)
            frontier = next_frontier
            if not frontier:
                break
        return {
            "entity": {
                "entity_id": target["entity_id"],
                "name": target["name"],
                "kind": target["kind"],
            },
            "via": via,
            "direction": direction,
            "depth": depth,
            "count": len(edges),
            "edges": edges,
        }

    @app.get("/api/conflicts")
    def conflicts() -> dict[str, Any]:
        items = database().conflicts()
        return {"count": len(items), "conflicts": items}

    @app.get("/api/doctor")
    def doctor() -> dict[str, Any]:
        return diagnose(root)

    @app.get("/api/stale/{entity_kind}")
    @app.get("/api/contradictions/{entity_kind}")
    def empty_diagnostics(entity_kind: str) -> dict[str, Any]:
        return {"entity_kind": entity_kind, "count": 0, "cells": []}

    @app.get("/api/stale-derivations")
    def stale_derivations() -> dict[str, Any]:
        return {"count": 0, "stale_derivations": []}

    @app.get("/api/question-challenges")
    def question_challenges(
        question: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        return database().question_challenges(question=question, status=status)

    @app.post("/api/question-challenges/{challenge_id}/resolve")
    def resolve_question_challenge(
        challenge_id: str, body: ChallengeResolveCreate
    ) -> dict[str, str]:
        database().resolve_question_challenge(
            challenge_id,
            body.status,
            body.resolution,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"challenge_id": challenge_id, "status": body.status}

    @app.get("/api/research/jobs")
    def research_jobs() -> list[dict[str, Any]]:
        with app.state.research_lock:
            return list(app.state.research_jobs.values())

    @app.get("/api/curator")
    def curator_status() -> dict[str, Any] | None:
        with app.state.research_lock:
            return app.state.curator

    @app.post("/api/curator", status_code=202)
    def launch_curator(body: CuratorCreate) -> dict[str, Any]:
        database().project()
        planning_cost, _ = curator_cost_rates()
        if body.budget_usd < planning_cost:
            raise EpiqError(
                "curator_budget_too_small",
                f"The curation strategy alone is estimated at ${planning_cost:.2f}",
                f"Choose a budget of at least ${planning_cost:.2f}.",
            )
        with app.state.research_lock:
            existing = app.state.curator
            if existing and existing.get("status") in {
                "planning",
                "awaiting_approval",
                "running",
                "stopping",
            }:
                raise EpiqError("curator_active", "A curator is already running for this project")
            curator_id = uuid.uuid4().hex[:16]
            app.state.curator = {
                "curator_id": curator_id,
                "objective": body.objective,
                "budget_usd": body.budget_usd,
                "estimated_spend_usd": 0.0,
                "reserved_usd": planning_cost,
                "cost_basis": "estimated",
                "cost_rates": {
                    "planning_cycle_usd": curator_cost_rates()[0],
                    "research_cell_usd": curator_cost_rates()[1],
                },
                "status": "planning",
                "max_cycles": body.max_cycles,
                "max_research_cells": body.max_research_cells,
                "completed_cycles": 0,
                "research_cells_used": 0,
                "no_change_streak": 0,
                "active_job_id": None,
                "pause_requested": False,
                "stop_requested": False,
                "cycles": [],
                "strategy": None,
                "message": "Pi is designing a curation strategy.",
                "created_at": now(),
            }
            save_curator()
            state = dict(app.state.curator)
        start_curator_strategy_thread(curator_id)
        return state

    @app.post("/api/curator/approve", status_code=202)
    def approve_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") != "awaiting_approval":
                raise EpiqError(
                    "curator_strategy_unavailable",
                    "The curator strategy is not awaiting approval",
                )
            state.update(
                status="running",
                pause_requested=False,
                stop_requested=False,
                message="Starting the first approved enrichment cycle.",
            )
            save_curator()
            curator_id = str(state["curator_id"])
            response = dict(state)
        start_curator_thread(curator_id)
        return response

    @app.post("/api/curator/add-budget", status_code=202)
    def add_curator_budget(body: CuratorBudgetAdd) -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") in {"planning", "running", "stopping"}:
                raise EpiqError(
                    "curator_active",
                    "Wait for the current curator cycle before adding budget",
                )
            if not state.get("strategy") or state.get("budget_usd") is None:
                raise EpiqError(
                    "curator_strategy_unavailable",
                    "Create and approve a budgeted curator strategy first",
                )
            state["budget_usd"] = round(
                float(state["budget_usd"]) + body.amount_usd,
                4,
            )
            if int(state.get("completed_cycles", 0)) >= int(state["max_cycles"]):
                state["max_cycles"] = int(state["max_cycles"]) + 10
            if int(state.get("research_cells_used", 0)) >= int(
                state["max_research_cells"]
            ):
                state["max_research_cells"] = int(state["max_research_cells"]) + 150
            state.update(
                status="running",
                pause_requested=False,
                stop_requested=False,
                no_change_streak=0,
                message=f"Added ${body.amount_usd:.2f}; continuing the approved strategy.",
            )
            if body.direction:
                state["continuation_direction"] = body.direction.strip()
            state.pop("finished_at", None)
            save_curator()
            curator_id = str(state["curator_id"])
            response = dict(state)
        start_curator_thread(curator_id)
        return response

    @app.post("/api/curator/pause", status_code=202)
    def pause_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") == "running":
                state["pause_requested"] = True
                state["message"] = "Will pause after the current cycle."
                save_curator()
            return dict(state)

    @app.post("/api/curator/resume", status_code=202)
    def resume_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") != "paused":
                raise EpiqError("curator_not_paused", "The curator is not paused")
            state.update(
                status="running",
                pause_requested=False,
                stop_requested=False,
                message="Resuming enrichment.",
            )
            save_curator()
            curator_id = str(state["curator_id"])
            response = dict(state)
        start_curator_thread(curator_id)
        return response

    @app.post("/api/curator/stop", status_code=202)
    def stop_curator() -> dict[str, Any]:
        with app.state.research_lock:
            state = app.state.curator
            if state is None:
                raise EpiqError("curator_not_found", "No curator is configured for this project")
            if state.get("status") in {
                "planning",
                "awaiting_approval",
                "running",
                "paused",
            }:
                state["stop_requested"] = True
                state["pause_requested"] = False
                if state.get("status") in {"paused", "awaiting_approval"}:
                    state.update(status="stopped", finished_at=now())
                else:
                    state["status"] = "stopping"
                state["message"] = "Stopping after the current safe boundary."
                save_curator()
            return dict(state)

    @app.post("/api/workspace-agent/jobs", status_code=202)
    def launch_workspace_agent(body: WorkspaceAgentCreate) -> dict[str, Any]:
        database().project()
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "job_type": "workspace_agent",
            "entity_kind": "Workspace",
            "question_id": "",
            "mode": "workspace_agent",
            "instructions": body.message,
            "user_message": body.message,
            "auto_approve": body.auto_approve,
            "status": "queued",
            "total": 0,
            "completed": 0,
            "target_entity_ids": [],
            "created_at": now(),
            "error": None,
            "messages": [{"at": now(), "message": "Workspace agent queued"}],
        }
        with app.state.research_lock:
            app.state.research_jobs[job_id] = job
        threading.Thread(
            target=plan_workspace,
            args=(job_id, body.message),
            daemon=True,
            name=f"epiq-{job_id}",
        ).start()
        return job

    @app.post("/api/workspace-agent/jobs/{job_id}/approve", status_code=202)
    def approve_workspace_agent(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("workspace_job_not_found", f"Workspace job not found: {job_id}")
            if job.get("approval_status") != "pending":
                raise EpiqError(
                    "workspace_plan_unavailable", "This workspace plan is not awaiting approval"
                )
            job["approval_status"] = "applying"
        try:
            apply_workspace_plan(job_id)
        except Exception:
            update_job(job_id, approval_status="pending")
            raise
        with app.state.research_lock:
            return app.state.research_jobs[job_id]

    @app.post("/api/workspace-agent/jobs/{job_id}/reject")
    def reject_workspace_agent(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("workspace_job_not_found", f"Workspace job not found: {job_id}")
            if job.get("approval_status") != "pending":
                raise EpiqError(
                    "workspace_plan_unavailable", "This workspace plan is not awaiting approval"
                )
        database().review_proposal(
            job["proposal_id"],
            "rejected",
            "Dismissed in the workspace agent console",
            actor="human:web",
            idempotency_key=f"{job_id}:reject",
        )
        update_job(job_id, approval_status="rejected", finished_at=now())
        with app.state.research_lock:
            return app.state.research_jobs[job_id]

    @app.post("/api/research/jobs/{job_id}/cancel")
    def cancel_research_job(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("research_job_not_found", f"Research job not found: {job_id}")
            job["cancel_requested"] = True
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["finished_at"] = now()
            return job

    @app.post("/api/research/not-found", status_code=201)
    def not_found(body: NotFoundCreate) -> dict[str, str]:
        result = database().record_not_found(
            body.subject,
            body.question,
            body.query,
            body.notes,
            actor=body.actor,
            idempotency_key=body.idempotency_key,
        )
        return {"task_id": result["outcome_id"], "state": "NotFound"}

    if frontend_path.exists():
        assets = frontend_path / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            candidate = (frontend_path / path).resolve()
            if not candidate.is_relative_to(frontend_path):
                raise HTTPException(status_code=404, detail="Not found")
            if not candidate.is_file():
                candidate = (frontend_path / "index.html").resolve()
            if not candidate.is_relative_to(frontend_path) or not candidate.is_file():
                raise HTTPException(status_code=404, detail="Not found")
            headers = {"Cache-Control": "no-store"} if candidate.name == "index.html" else None
            return FileResponse(candidate, headers=headers)

    return app


def _web_matrix(projection: dict[str, Any], database: LedgerDatabase) -> dict[str, Any]:
    questions = projection["questions"]
    entities = {item["entity_id"]: item for item in database.entities()}
    derivations = {item["claim_id"]: item for item in database.derivations()}
    outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in database.research_outcomes(kind=projection["kind"]):
        outcomes.setdefault((item["subject_id"], item["question_id"]), []).append(item)
    evidence = {
        item["evidence_id"]: item
        for item in database.evidence(
            evidence_ids=[
                evidence_id
                for entity_cells in projection["cells"].values()
                for claims in entity_cells.values()
                for claim in claims
                for evidence_id in claim["evidence_ids"]
            ]
        )
    }
    rows = []
    for entity in projection["entities"]:
        cells: dict[str, Any] = {}
        for question in questions:
            claims = projection["cells"][entity["entity_id"]][question["question_id"]]
            values = [claim["value"] for claim in claims]
            attempts = outcomes.get((entity["entity_id"], question["question_id"]), [])
            state = (
                "Answered"
                if len(claims) == 1
                else "Contested"
                if claims
                else "NotFound"
                if attempts
                else "Unasked"
            )
            lineage = []
            for claim in claims:
                derivation = derivations.get(claim["claim_id"])
                reviews = claim["reviews"]
                review = reviews[0] if len(reviews) == 1 else None
                for evidence_id in claim["evidence_ids"]:
                    source = evidence[evidence_id]
                    lineage.append(
                        {
                            "token": f"{claim['claim_id']}:{evidence_id}",
                            "claim_id": claim["claim_id"],
                            "value": claim["value"],
                            "confidence": claim["confidence"],
                            "epistemic_status": claim["epistemic_status"],
                            "estimate": claim["estimate"],
                            "evidence_id": evidence_id,
                            "source": {
                                "title": source["title"],
                                "url": source["url"],
                                "published_at": source["published_at"],
                                "retrieved_at": source["retrieved_at"],
                            },
                            "excerpt": source["excerpt"],
                            "as_of": claim["valid_from"],
                            "temporal_basis": claim["temporal_basis"],
                            "review": (
                                {
                                    "status": review["decision"],
                                    "reason": review["reason"],
                                }
                                if review
                                else {"status": "unreviewed"}
                            ),
                            "derivation": (
                                {
                                    "operation": derivation["operation"],
                                    "parameters": derivation["parameters"],
                                    "input_claim_ids": [
                                        claim_id
                                        for values in derivation["inputs"].values()
                                        for claim_id in values
                                    ],
                                    "dependencies": [
                                        {"claim_id": claim_id, "role": role}
                                        for role, claim_ids in derivation["inputs"].items()
                                        for claim_id in claim_ids
                                    ],
                                }
                                if derivation
                                else None
                            ),
                        }
                    )
            cell = {
                "state": state,
                "value": values[0] if len(values) == 1 else None,
                "values": values,
                "claims": claims,
                "lineage": lineage,
                "research": (
                    {
                        "task_id": ",".join(item["outcome_id"] for item in attempts),
                        "query": "\n".join(dict.fromkeys(item["query"] for item in attempts)),
                        "notes": "\n".join(dict.fromkeys(item["notes"] for item in attempts)),
                        "attempts": attempts,
                    }
                    if attempts and not claims
                    else None
                ),
            }
            if str(question["value_type"]).startswith("Ref[") and values:
                identifiers = [
                    identifier
                    for value in values
                    for identifier in (value if isinstance(value, list) else [value])
                ]
                target_kind = str(question["value_type"])[4:-1]
                cell["references"] = [
                    _web_reference(identifier, entities, target_kind) for identifier in identifiers
                ]
            cells[question["name"]] = cell
        rows.append({**entity, "cells": cells})
    return {"entity_kind": projection["kind"], "questions": questions, "rows": rows}


def _web_reference(
    identifier: Any, entities: dict[str, dict[str, Any]], target_kind: str
) -> dict[str, str]:
    """Present resolved IDs and unresolved structured relationship findings safely."""
    if isinstance(identifier, dict):
        entity_id = identifier.get("entity_id")
        entity = entities.get(entity_id, {}) if isinstance(entity_id, str) else {}
        return {
            "entity_id": entity_id or "",
            "kind": str(entity.get("kind") or target_kind),
            "name": str(entity.get("name") or identifier.get("name") or "Unresolved relationship"),
        }
    entity = entities.get(identifier, {})
    return {
        "entity_id": str(identifier),
        "kind": str(entity.get("kind") or target_kind),
        "name": str(entity.get("name", identifier)),
    }
