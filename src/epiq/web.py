"""FastAPI application exposing an Epiq database to the spreadsheet UI."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .cli import capabilities
from .dsl import describe, parse
from .edsl_export import write_edsl
from .errors import EpiqError
from .html import write_html
from .operations import agent_operation_catalog
from .research import (
    EntitySuggestionRunner,
    FieldSuggestionRunner,
    OpenAIEntitySuggestionRunner,
    OpenAIFieldSuggestionRunner,
    OpenAIResearchRunner,
    OpenAIWorkspaceAgentRunner,
    ResearchRunner,
    WorkspaceAgentRunner,
)
from .store import LATEST_SCHEMA_VERSION, Store
from .xlsx import write_xlsx


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)


class ProjectOpen(BaseModel):
    project_id: str


class RowQueryCreate(BaseModel):
    predicates: list[dict[str, Any]] = Field(default_factory=list)
    known_at: str | None = None
    valid_at: str | None = None


class ApplyCreate(BaseModel):
    document: dict[str, Any]
    actor: str = "human:web"


class EntityCreate(BaseModel):
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict)
    actor: str = "human:web"


class EntityKindCreate(BaseModel):
    kind: str = Field(min_length=1)
    actor: str = "human:web"


class EntityAliasCreate(BaseModel):
    alias: str = Field(min_length=1)
    actor: str = "human:web"


class EntityMergeCreate(BaseModel):
    destination: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class EntityVisibilityCreate(BaseModel):
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class QuestionCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    subject_kind: str = Field(min_length=1)
    value_type: str = Field(min_length=1)
    definition: dict[str, Any] = Field(default_factory=dict)
    actor: str = "human:web"


class QuestionPolicyCreate(BaseModel):
    volatility: Literal["stable", "slow", "dynamic"]
    freshness_days: int | None = Field(default=None, ge=1)
    actor: str = "human:web"


class QuestionRevisionCreate(BaseModel):
    value_type: str = Field(min_length=1)
    definition: dict[str, Any]
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class SchemaAdaptationAcceptCreate(BaseModel):
    actor: str = "human:web"


class QuestionVisibilityCreate(BaseModel):
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class QuestionReplacement(BaseModel):
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    value_type: str = Field(min_length=1)
    subject_kind: str | None = None
    definition: dict[str, Any] = Field(default_factory=dict)


class QuestionEvolutionCreate(BaseModel):
    replacements: list[QuestionReplacement] = Field(min_length=1)
    relationship: Literal["replaces", "splits", "refines"]
    reason: str = Field(min_length=1)
    retire_predecessor: bool = True
    actor: str = "human:web"


class QuestionChallengeCreate(BaseModel):
    problem: str
    explanation: str = Field(min_length=1)
    example_entity: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    proposed_replacement: dict[str, Any] | None = None
    actor: str = "human:web"


class QuestionChallengeResolve(BaseModel):
    status: Literal["resolved", "dismissed"]
    resolution: str = Field(min_length=1)
    actor: str = "human:web"


class EvidenceCreate(BaseModel):
    source_type: Literal["web", "personal", "model", "report", "interview", "other"] = "web"
    url: str | None = None
    title: str = Field(min_length=1)
    retrieved_at: str = Field(min_length=1)
    published_at: str | None = None
    excerpt: str = Field(min_length=1)
    actor: str = "human:web"


class EvidenceAssessmentCreate(BaseModel):
    status: Literal["accepted", "disputed", "invalid", "superseded"]
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class ClaimCreate(BaseModel):
    subject: str
    question: str
    value: Any
    valid_from: str
    evidence_ids: list[str] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"] = "high"
    temporal_basis: Literal["observed", "source", "unknown"] = "observed"
    actor: str = "human:web"


class ClaimBatchCreate(BaseModel):
    claims: list[ClaimCreate] = Field(min_length=1, max_length=1000)
    actor: str = "agent:web"


class WriteBatchCreate(BaseModel):
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=2000)
    actor: str = "agent:web"


class ClaimProposalCreate(ClaimCreate):
    rationale: str = ""


class ClaimProposalReview(BaseModel):
    proposal_ids: list[str] = Field(min_length=1)
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class RetractionCreate(BaseModel):
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class ValidityEndCreate(BaseModel):
    valid_to: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    actor: str = "human:web"


class SupersedeCreate(BaseModel):
    value: Any
    valid_from: str
    evidence_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)
    confidence: Literal["low", "medium", "high"] = "high"
    temporal_basis: Literal["observed", "source", "unknown"] = "observed"
    actor: str = "human:web"


class ClaimChallengeCreate(BaseModel):
    reason: str = Field(min_length=1)
    research_guidance: str = ""
    retract: bool = True
    actor: str = "human:web"


class NotFoundCreate(BaseModel):
    subject: str
    question: str
    query: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    actor: str = "human:web"


class ResearchFeedbackCreate(BaseModel):
    reason: str = Field(min_length=1)
    research_guidance: str = ""
    save_to_field: bool = True
    actor: str = "human:web"


class ResearchCreate(BaseModel):
    entity_kind: str
    question: str
    mode: Literal["fill_missing", "add_evidence", "retry_not_found"] = "fill_missing"
    instructions: str = ""
    entity_ids: list[str] | None = None
    scope: Literal["cell", "row", "column", "table"] = "column"


class RowResearchCreate(BaseModel):
    entity_kind: str
    entity_id: str
    instructions: str = ""


class TableResearchCreate(BaseModel):
    entity_kind: str
    instructions: str = ""


class ResearchCancelScope(BaseModel):
    scope: Literal["cell", "row", "column", "table"]
    entity_kind: str
    entity_id: str | None = None
    question_id: str | None = None


class SuggestEntitiesCreate(BaseModel):
    entity_kind: str = Field(min_length=1)
    count: int = Field(default=5, ge=1, le=20)
    instructions: str = ""


class AcceptSuggestionCreate(BaseModel):
    suggestion_id: str
    actor: str = "human:web"


class AcceptRelationshipSuggestionsCreate(BaseModel):
    suggestion_ids: list[str] = Field(min_length=1)
    actor: str = "human:web"


class RelationshipReviewScope(BaseModel):
    scope: Literal["cell", "column", "table"]
    entity_kind: str | None = None
    subject_entity_id: str | None = None
    question_id: str | None = None
    review_id: str = Field(default_factory=lambda: f"rrv_{uuid.uuid4().hex}")
    reason: str = "Reviewed provisional relationship research"
    actor: str = "human:web"


class SuggestFieldsCreate(BaseModel):
    entity_kind: str = Field(min_length=1)
    count: int = Field(default=5, ge=1, le=20)
    instructions: str = ""


class WorkspaceAgentCreate(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class AcceptFieldSuggestionsCreate(BaseModel):
    suggestion_ids: list[str] = Field(min_length=1)
    actor: str = "human:web"


class AggregateCreate(BaseModel):
    question: str
    operation: Literal["count", "sum", "avg", "min", "max"]
    group_by: str | None = None


class DeriveCreate(BaseModel):
    subject: str
    question: str
    operation: Literal[
        "sum",
        "avg",
        "min",
        "max",
        "count",
        "divide",
        "expression",
        "weighted_avg",
        "linear",
    ]
    input_claim_ids: list[str] = Field(default_factory=list)
    input_cells: list[tuple[str, str]] = Field(default_factory=list)
    weight_cells: list[tuple[str, str]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    valid_from: str
    confidence: Literal["low", "medium", "high"] = "medium"


class DistributionDeriveCreate(BaseModel):
    subject: str
    question: str
    input_claim_ids: list[str] = Field(min_length=1)
    weights: list[float] | None = None
    valid_from: str
    confidence: Literal["low", "medium", "high"] = "medium"
    actor: str = "agent:api"


class EpiQLCheckCreate(BaseModel):
    source: str = Field(min_length=1)
    actor: str = "human:web"


class MaterializeCreate(BaseModel):
    entity_kind: str
    valid_from: str
    subjects: list[str] | None = None
    question: str | None = None
    actor: str = "human:web"


class PropagateCreate(BaseModel):
    subject: str
    source_question: str
    target_question: str
    valid_from: str
    via: str | None = None
    direction: Literal["incoming", "outgoing"] = "outgoing"
    depth: int = Field(default=1, ge=1, le=20)
    confidence: Literal["low", "medium", "high"] = "medium"
    actor: str = "human:web"


def create_app(
    database: str | Path | None = None,
    frontend: str | Path | None = None,
    research_runner: ResearchRunner | None = None,
    suggestion_runner: EntitySuggestionRunner | None = None,
    field_suggestion_runner: FieldSuggestionRunner | None = None,
    workspace_agent_runner: WorkspaceAgentRunner | None = None,
    projects_directory: str | Path | None = None,
) -> FastAPI:
    """Create an application bound to one local Epiq database."""
    database_path = Path(database or os.environ.get("EPIQ_DB", ".epiq/epiq.sqlite"))
    projects_path = Path(
        projects_directory or os.environ.get("EPIQ_PROJECTS_DIR", ".epiq/projects")
    ).resolve()
    frontend_path = Path(frontend or Path(__file__).parents[2] / "web" / "dist")
    app = FastAPI(title="Epiq", version="0.1.0")
    app.state.database: Path | None = database_path.resolve()
    app.state.initial_database = database_path.resolve()
    app.state.projects_path = projects_path
    app.state.frontend = frontend_path
    app.state.research_runner = research_runner or OpenAIResearchRunner()
    app.state.suggestion_runner = suggestion_runner or OpenAIEntitySuggestionRunner()
    app.state.field_suggestion_runner = field_suggestion_runner or OpenAIFieldSuggestionRunner()
    app.state.workspace_agent_runner = workspace_agent_runner or OpenAIWorkspaceAgentRunner()
    app.state.research_jobs = {}
    app.state.research_lock = threading.Lock()
    app.state.research_semaphore = threading.Semaphore(
        int(os.environ.get("EPIQ_RESEARCH_CONCURRENCY", "6"))
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def load_persisted_jobs() -> None:
        jobs: dict[str, dict[str, Any]] = {}
        if app.state.database is not None and app.state.database.exists():
            for job in Store(app.state.database).agent_jobs():
                if job.get("status") in {"queued", "running"}:
                    job["status"] = "failed"
                    job["error"] = "Server stopped before this job completed; launch it again."
                    job["finished_at"] = datetime.now(UTC).isoformat()
                    job.setdefault("messages", []).append(
                        {
                            "at": datetime.now(UTC).isoformat(),
                            "message": "Marked interrupted during server startup",
                        }
                    )
                    Store(app.state.database).save_agent_job(job)
                jobs[str(job["job_id"])] = job
        app.state.research_jobs = jobs

    def persist_job(job_id: str) -> None:
        if app.state.database is not None:
            Store(app.state.database).save_agent_job(app.state.research_jobs[job_id])

    load_persisted_jobs()

    @app.middleware("http")
    async def disable_local_ui_cache(request: Request, call_next):
        """Local development should always expose the currently built frontend."""
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    def store(require_existing: bool = True) -> Store:
        if app.state.database is None:
            raise EpiqError(
                "no_active_project",
                "No project is currently open",
                "Create a new project or open an existing project.",
            )
        result = Store(app.state.database)
        if require_existing and not result.path.exists():
            raise EpiqError(
                "project_not_found",
                f"Database does not exist: {result.path}",
                "Initialize it from the welcome screen or run epiq init.",
            )
        return result

    def active_jobs() -> bool:
        with app.state.research_lock:
            return any(
                job["status"] in {"queued", "running"} for job in app.state.research_jobs.values()
            )

    def available_projects() -> list[dict[str, Any]]:
        candidates = {app.state.initial_database}
        if app.state.projects_path.exists():
            candidates.update(app.state.projects_path.glob("*.sqlite"))
        if app.state.database is not None:
            candidates.add(app.state.database)
        projects = []
        for path in sorted(candidates):
            if not path.exists():
                continue
            try:
                overview = Store(path).overview()
                name = overview["project"].get("name", path.stem)
            except Exception:
                continue
            projects.append(
                {
                    "project_id": sha256(str(path.resolve()).encode()).hexdigest()[:16],
                    "name": name,
                    "path": str(path.resolve()),
                    "active": app.state.database == path.resolve(),
                }
            )
        return projects

    @app.exception_handler(EpiqError)
    async def domain_error(_request: Request, error: EpiqError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": error.as_dict()})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """Cheap liveness plus project readiness for local process supervision."""
        if app.state.database is None:
            return {"ok": True, "project": "closed", "database": None, "initialized": False}
        if not app.state.database.exists():
            return {
                "ok": True,
                "project": "uninitialized",
                "database": str(app.state.database),
                "initialized": False,
            }
        overview = store().overview()
        return {
            "ok": True,
            "project": "ready",
            "database": str(app.state.database),
            "initialized": True,
            "schema_version": overview["project"]["schema_version"],
        }

    @app.get("/api/capabilities")
    def api_capabilities(
        command: str | None = None, include_schema: bool = False
    ) -> dict[str, Any]:
        result = capabilities(command)
        if include_schema:
            project_store = store()
            overview = project_store.overview()
            result["project_schema"] = {
                "project": overview["project"],
                "tables": [
                    {
                        "entity_kind": item["kind"],
                        "questions": project_store.matrix(str(item["kind"]))["questions"],
                    }
                    for item in overview["entity_kinds"]
                ],
            }
        return result

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        return available_projects()

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        if active_jobs():
            raise EpiqError("research_active", "Wait for active research before switching projects")
        slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or "project"
        app.state.projects_path.mkdir(parents=True, exist_ok=True)
        path = app.state.projects_path / f"{slug}.sqlite"
        suffix = 2
        while path.exists():
            path = app.state.projects_path / f"{slug}-{suffix}.sqlite"
            suffix += 1
        Store(path).initialize(body.name)
        app.state.database = path.resolve()
        with app.state.research_lock:
            load_persisted_jobs()
        return next(item for item in available_projects() if item["active"])

    @app.post("/api/projects/import", status_code=201)
    async def import_project(
        request: Request, filename: str = Query(min_length=1)
    ) -> dict[str, Any]:
        """Import an uploaded Epiq SQLite database into managed project storage."""
        if active_jobs():
            raise EpiqError("research_active", "Wait for active research before switching projects")
        source_name = Path(filename).name
        if Path(source_name).suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
            raise EpiqError(
                "invalid_project_file",
                "Select a SQLite database ending in .sqlite, .sqlite3, or .db",
            )
        maximum_bytes = int(os.environ.get("EPIQ_IMPORT_MAX_BYTES", str(100 * 1024 * 1024)))
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise EpiqError("invalid_request", "Content-Length must be an integer") from error
            if declared_size > maximum_bytes:
                raise EpiqError(
                    "project_too_large",
                    f"Project exceeds the {maximum_bytes // (1024 * 1024)} MB import limit",
                )
        app.state.projects_path.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            prefix=".epiq-import-",
            suffix=".sqlite",
            dir=app.state.projects_path,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > maximum_bytes:
                    temporary_path.unlink(missing_ok=True)
                    raise EpiqError(
                        "project_too_large",
                        f"Project exceeds the {maximum_bytes // (1024 * 1024)} MB import limit",
                    )
                temporary.write(chunk)
        try:
            with temporary_path.open("rb") as uploaded:
                signature = uploaded.read(16)
            if size < 100 or signature != b"SQLite format 3\x00":
                raise EpiqError(
                    "invalid_project_file", "The selected file is not a SQLite database"
                )
            try:
                connection = sqlite3.connect(f"file:{temporary_path}?mode=ro", uri=True)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if integrity != "ok":
                    raise EpiqError(
                        "invalid_project_file", f"SQLite integrity check failed: {integrity}"
                    )
                required = {"meta", "events", "entities", "questions", "claims"}
                if not required.issubset(tables):
                    raise EpiqError(
                        "not_epiq_project",
                        "The SQLite database is valid but is not an initialized Epiq project",
                    )
                metadata = {
                    str(row["key"]): str(row["value"])
                    for row in connection.execute("SELECT key, value FROM meta")
                }
                if not {"project_id", "name", "schema_version"}.issubset(metadata):
                    raise EpiqError(
                        "not_epiq_project", "The database is missing Epiq project metadata"
                    )
                try:
                    schema_version = int(metadata["schema_version"])
                except ValueError as error:
                    raise EpiqError(
                        "invalid_project_file", "The Epiq schema version is invalid"
                    ) from error
                if schema_version > LATEST_SCHEMA_VERSION:
                    raise EpiqError(
                        "schema_too_new",
                        f"Database schema v{schema_version} is newer than supported "
                        f"v{LATEST_SCHEMA_VERSION}",
                        "Upgrade Epiq before importing this project.",
                    )
            except sqlite3.DatabaseError as error:
                raise EpiqError(
                    "invalid_project_file", f"Could not read the SQLite database: {error}"
                ) from error
            finally:
                if "connection" in locals():
                    connection.close()

            slug = re.sub(r"[^a-z0-9]+", "-", metadata["name"].lower()).strip("-")
            slug = slug or re.sub(r"[^a-z0-9]+", "-", Path(source_name).stem.lower()).strip("-")
            path = app.state.projects_path / f"{slug or 'imported-project'}.sqlite"
            suffix = 2
            while path.exists():
                path = app.state.projects_path / f"{slug or 'imported-project'}-{suffix}.sqlite"
                suffix += 1
            os.replace(temporary_path, path)
            app.state.database = path.resolve()
            # Opening the managed copy applies any supported forward-only schema migrations.
            Store(app.state.database).overview()
            with app.state.research_lock:
                load_persisted_jobs()
            return next(item for item in available_projects() if item["active"])
        finally:
            temporary_path.unlink(missing_ok=True)

    @app.post("/api/projects/open")
    def open_project(body: ProjectOpen) -> dict[str, Any]:
        if active_jobs():
            raise EpiqError("research_active", "Wait for active research before switching projects")
        project = next(
            (item for item in available_projects() if item["project_id"] == body.project_id),
            None,
        )
        if project is None:
            raise EpiqError("project_not_found", "The selected project is no longer available")
        app.state.database = Path(project["path"])
        with app.state.research_lock:
            load_persisted_jobs()
        return project

    @app.post("/api/projects/close")
    def close_project() -> dict[str, bool]:
        if active_jobs():
            raise EpiqError(
                "research_active", "Wait for active research before closing this project"
            )
        app.state.database = None
        with app.state.research_lock:
            app.state.research_jobs = {}
        return {"closed": True}

    @app.post("/api/project", status_code=201)
    def initialize(body: ProjectCreate) -> dict[str, Any]:
        project = store(require_existing=False)
        project.initialize(body.name)
        return project.overview()

    @app.get("/api/project")
    def project() -> dict[str, Any]:
        return store().overview()

    @app.get("/api/schema")
    def schema(entity_kind: str | None = None) -> dict[str, Any]:
        project_store = store()
        overview = project_store.overview()
        kinds = [str(item["kind"]) for item in overview["entity_kinds"]]
        selected = [entity_kind] if entity_kind else kinds
        return {
            "project": overview["project"],
            "value_types": capabilities()["value_types"],
            "tables": [
                {"entity_kind": kind, "questions": project_store.matrix(kind)["questions"]}
                for kind in selected
            ],
        }

    @app.post("/api/epiql/check")
    def check_epiql(body: EpiQLCheckCreate) -> dict[str, Any]:
        return {"ok": True, "program": describe(parse(body.source))}

    @app.get("/api/context")
    def context(
        entity_kind: str | None = None, budget: int = Query(default=4000, ge=100)
    ) -> dict[str, Any]:
        project_store = store()
        overview = project_store.overview()
        kinds = [str(item["kind"]) for item in overview["entity_kinds"]]
        tables = [project_store.matrix(kind) for kind in ([entity_kind] if entity_kind else kinds)]
        result: dict[str, Any] = {
            "project": overview["project"],
            "tables": tables,
            "truncated": False,
        }
        if len(json.dumps(result, sort_keys=True)) <= budget * 4:
            return result
        compact_tables = []
        used = 0
        for table in tables:
            compact: dict[str, Any] = {
                "entity_kind": table["entity_kind"],
                "questions": [
                    {key: question[key] for key in ("name", "value_type", "definition")}
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
                            "confidence": cell.get("confidence"),
                        }
                        for name, cell in row["cells"].items()
                    },
                }
                size = len(json.dumps(candidate, sort_keys=True))
                if used + size > budget * 4:
                    break
                compact["rows"].append(candidate)
                used += size
            compact_tables.append(compact)
        return {
            "project": overview["project"],
            "tables": compact_tables,
            "truncated": True,
            "approximate_token_budget": budget,
        }

    @app.post("/api/apply")
    def apply_document(body: ApplyCreate) -> dict[str, Any]:
        return store().apply_document(body.document, body.actor)

    @app.get("/api/doctor")
    def doctor() -> dict[str, Any]:
        return store().doctor()

    @app.get("/api/export/project.sqlite")
    def export_project_database() -> FileResponse:
        project_store = store()
        with NamedTemporaryFile(
            prefix="epiq-project-", suffix=".sqlite", delete=False
        ) as temporary:
            output = Path(temporary.name)
        project_store.backup(output, overwrite=True)
        project_name = str(project_store.overview()["project"].get("name", "epiq-project"))
        filename = f"{re.sub(r'[^a-zA-Z0-9._-]+', '-', project_name).strip('-')}.sqlite"
        return FileResponse(
            output,
            filename=filename or "epiq-project.sqlite",
            media_type="application/vnd.sqlite3",
            background=BackgroundTask(output.unlink, missing_ok=True),
        )

    @app.get("/api/export/project.epiq")
    def export_project_bundle() -> FileResponse:
        project_store = store()
        with NamedTemporaryFile(prefix="epiq-project-", suffix=".epiq", delete=False) as temporary:
            output = Path(temporary.name)
        project_store.export_bundle(output, overwrite=True)
        project_name = str(project_store.overview()["project"].get("name", "epiq-project"))
        filename = f"{re.sub(r'[^a-zA-Z0-9._-]+', '-', project_name).strip('-')}.epiq"
        return FileResponse(
            output,
            filename=filename or "epiq-project.epiq",
            media_type="application/zip",
            background=BackgroundTask(output.unlink, missing_ok=True),
        )

    @app.get("/api/matrix/{entity_kind}")
    def matrix(
        entity_kind: str,
        questions: Annotated[list[str] | None, Query()] = None,
        known_at: str | None = None,
        valid_at: str | None = None,
    ) -> dict[str, Any]:
        return store().matrix(entity_kind, questions, known_at, valid_at)

    @app.post("/api/query/{entity_kind}")
    def query_rows(entity_kind: str, body: RowQueryCreate) -> dict[str, Any]:
        return store().query_rows(entity_kind, body.predicates, body.known_at, body.valid_at)

    def diagnostic_cells(entity_kind: str, diagnostic: str) -> dict[str, Any]:
        projection = store().matrix(entity_kind)
        cells = []
        for row in projection["rows"]:
            for question in projection["questions"]:
                cell = row["cells"][question["name"]]
                include = {
                    "gaps": cell["state"] in {"Unasked", "NotFound"},
                    "stale": cell.get("temporal", {}).get("freshness") == "stale",
                    "contradictions": cell["state"] == "Contested",
                }[diagnostic]
                if include:
                    cells.append(
                        {
                            "entity_id": row["entity_id"],
                            "entity_name": row["name"],
                            "question_id": question["question_id"],
                            "question": question["name"],
                            "state": cell["state"],
                            "values": cell.get("values", []),
                            "lineage": cell.get("lineage", []),
                            "temporal": cell.get("temporal"),
                        }
                    )
        return {"entity_kind": entity_kind, "count": len(cells), "cells": cells}

    @app.get("/api/gaps/{entity_kind}")
    def gaps(entity_kind: str) -> dict[str, Any]:
        return diagnostic_cells(entity_kind, "gaps")

    @app.get("/api/stale/{entity_kind}")
    def stale(entity_kind: str) -> dict[str, Any]:
        return diagnostic_cells(entity_kind, "stale")

    @app.get("/api/contradictions/{entity_kind}")
    def contradictions(entity_kind: str) -> dict[str, Any]:
        return diagnostic_cells(entity_kind, "contradictions")

    @app.get("/api/refresh-plan/{entity_kind}")
    def refresh_plan(
        entity_kind: str,
        include: Literal["gaps", "stale", "contested", "all"] = "all",
    ) -> dict[str, Any]:
        projection = store().matrix(entity_kind)
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
                    if include == "all"
                    else [item for item in reasons if item == include.rstrip("s")]
                )
                if not allowed:
                    continue
                label = str(question["definition"].get("label", question["name"]))
                tasks.append(
                    {
                        "task_key": f"{row['entity_id']}:{question['question_id']}",
                        "entity_kind": entity_kind,
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
                            dict.fromkeys(item["source"]["url"] for item in cell.get("lineage", []))
                        ),
                    }
                )
        return {"entity_kind": entity_kind, "count": len(tasks), "tasks": tasks}

    @app.get("/api/stale-derivations")
    def stale_derivations(entity_kind: str | None = None) -> dict[str, Any]:
        return store().stale_derivations(entity_kind)

    @app.get("/api/search")
    def search(text: str, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        results = store().search(text, limit)
        return {"query": text, "count": len(results), "results": results}

    @app.post("/api/aggregate/{entity_kind}")
    def aggregate(entity_kind: str, body: AggregateCreate) -> dict[str, Any]:
        projection = store().matrix(entity_kind)
        question_names = {str(item["name"]) for item in projection["questions"]}
        for name in (body.question, body.group_by):
            if name and name not in question_names:
                raise EpiqError("question_not_found", f"Question not found: {name}")
        grouped: dict[str, list[Any]] = {}
        for row in projection["rows"]:
            values = row["cells"][body.question].get("values", [])
            if not values:
                continue
            keys = ["all"]
            if body.group_by:
                group_cell = row["cells"][body.group_by]
                keys = [
                    str(item.get("name", item)) if isinstance(item, dict) else str(item)
                    for item in group_cell.get("display_values", group_cell.get("values", []))
                ] or ["Unasked"]
            for key in keys:
                grouped.setdefault(key, []).extend(values)
        groups = []
        for key, values in sorted(grouped.items()):
            if body.operation == "count":
                value: Any = len(values)
            else:
                if not all(
                    isinstance(item, int | float) and not isinstance(item, bool) for item in values
                ):
                    raise EpiqError(
                        "non_numeric_aggregate", f"{body.question} contains non-numeric values"
                    )
                value = {
                    "sum": sum,
                    "avg": lambda items: sum(items) / len(items),
                    "min": min,
                    "max": max,
                }[body.operation](values)
            groups.append({"group": key, "value": value, "count": len(values)})
        return {
            "entity_kind": entity_kind,
            "question": body.question,
            "operation": body.operation,
            "groups": groups,
        }

    @app.get("/api/reports/dossier/{entity}")
    def dossier(entity: str) -> dict[str, Any]:
        return store().dossier(entity)

    @app.get("/api/related/{entity}")
    def related(
        entity: str,
        via: str | None = None,
        direction: str = "both",
        depth: int = 1,
    ) -> dict[str, Any]:
        return store().related(entity, via, direction, depth)

    @app.get("/api/reports/timeline/{entity_kind}/{question}")
    def timeline(entity_kind: str, question: str) -> dict[str, Any]:
        return store().timeline(entity_kind, question)

    @app.get("/api/reports/season-record/{season}")
    def season_record(
        season: str, known_at: str | None = None, valid_at: str | None = None
    ) -> dict[str, Any]:
        return store().season_record(season, known_at, valid_at)

    @app.post("/api/reports/delta")
    def delta_report(since_seq: int | None = None, actor: str = "human:web") -> dict[str, Any]:
        return store().delta_report(actor, since_seq)

    @app.get("/api/export/{entity_kind}.xlsx")
    def export_xlsx(entity_kind: str) -> FileResponse:
        matrix_data = store().matrix(entity_kind)
        with NamedTemporaryFile(prefix="epiq-", suffix=".xlsx", delete=False) as temporary:
            output = Path(temporary.name)
        write_xlsx(matrix_data, output, store().history())
        safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "-", entity_kind).strip("-") or "table"
        return FileResponse(
            output,
            filename=f"{safe_kind}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            background=BackgroundTask(output.unlink, missing_ok=True),
        )

    def html_export(entity_kind: str | None) -> FileResponse:
        with NamedTemporaryFile(prefix="epiq-", suffix=".html", delete=False) as temporary:
            output = Path(temporary.name)
        write_html(store(), output, entity_kind)
        safe_kind = (
            re.sub(r"[^A-Za-z0-9_-]+", "-", entity_kind).strip("-") if entity_kind else "project"
        )
        return FileResponse(
            output,
            filename=f"{safe_kind or 'project'}.html",
            media_type="text/html",
            background=BackgroundTask(output.unlink, missing_ok=True),
        )

    @app.get("/api/export/project.html")
    def export_project_html() -> FileResponse:
        return html_export(None)

    @app.get("/api/export/{entity_kind}.html")
    def export_html(entity_kind: str) -> FileResponse:
        return html_export(entity_kind)

    @app.get("/api/export/{entity_kind}.{object_type}.ep")
    def export_edsl(entity_kind: str, object_type: str) -> FileResponse:
        if object_type not in {"scenario-list", "agent-list"}:
            raise EpiqError(
                "invalid_edsl_type",
                "EDSL export type must be scenario-list or agent-list",
            )
        with NamedTemporaryFile(prefix="epiq-edsl-", suffix=".ep", delete=False) as temporary:
            output = Path(temporary.name)
        write_edsl(store().matrix(entity_kind), output, object_type)
        safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "-", entity_kind).strip("-") or "table"
        return FileResponse(
            output,
            filename=f"{safe_kind}.{object_type}.ep",
            media_type="application/zip",
            background=BackgroundTask(output.unlink, missing_ok=True),
        )

    @app.post("/api/entities", status_code=201)
    def add_entity(body: EntityCreate) -> dict[str, str]:
        entity_id = store().add_entity(body.kind, body.name, body.attributes, body.actor)
        return {"entity_id": entity_id}

    @app.post("/api/entities/{entity_id}/aliases", status_code=201)
    def add_entity_alias(entity_id: str, body: EntityAliasCreate) -> dict[str, str]:
        return {"alias_id": store().add_entity_alias(entity_id, body.alias, body.actor)}

    @app.get("/api/entities/duplicate-candidates")
    def duplicate_entity_candidates(kind: str = Query(min_length=1)) -> dict[str, Any]:
        projection = store().matrix(kind)
        rows = projection["rows"]

        def name_parts(name: str) -> tuple[str, str, list[str]]:
            normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
            base = re.split(r"\s*[,(]", name, maxsplit=1)[0]
            base = re.sub(r"[^a-z0-9]+", " ", base.casefold()).strip()
            tokens = base.split()
            return normalized, base, tokens

        def investigated(row: dict[str, Any]) -> int:
            return sum(cell.get("state") != "Unasked" for cell in row["cells"].values())

        candidates: list[dict[str, Any]] = []
        for left_index, left in enumerate(rows):
            left_full, left_base, left_tokens = name_parts(str(left["name"]))
            for right in rows[left_index + 1 :]:
                right_full, right_base, right_tokens = name_parts(str(right["name"]))
                reasons: list[str] = []
                if left_base == right_base:
                    score = 0.99
                    reasons.append("Names differ only by a location or qualifier")
                else:
                    ratio = SequenceMatcher(None, left_base, right_base).ratio()
                    shorter, longer = sorted((left_tokens, right_tokens), key=len)
                    containment = bool(shorter) and longer[: len(shorter)] == shorter
                    if containment and len(longer) - len(shorter) <= 1:
                        score = max(0.92, ratio)
                        reasons.append("One name adds a single descriptive word")
                    elif ratio >= 0.86:
                        score = ratio
                        reasons.append("Names are strongly similar")
                    else:
                        continue
                shared = 0
                conflicts = 0
                for question in projection["questions"]:
                    left_cell = left["cells"][question["name"]]
                    right_cell = right["cells"][question["name"]]
                    if left_cell.get("state") == "Unasked" or right_cell.get("state") == "Unasked":
                        continue
                    left_values = left_cell.get("values") or [left_cell.get("value")]
                    right_values = right_cell.get("values") or [right_cell.get("value")]
                    if json.dumps(left_values, sort_keys=True) == json.dumps(
                        right_values, sort_keys=True
                    ):
                        shared += 1
                    else:
                        conflicts += 1
                if shared:
                    reasons.append(
                        f"{shared} investigated field{'s' if shared != 1 else ''} agree"
                    )
                    score = min(1.0, score + min(shared, 3) * 0.01)
                left_specificity = len(left_full) + investigated(left) * 2
                right_specificity = len(right_full) + investigated(right) * 2
                survivor = right if right_specificity > left_specificity else left
                duplicate = left if survivor is right else right
                candidates.append(
                    {
                        "candidate_id": f"dup_{left['entity_id']}_{right['entity_id']}",
                        "score": round(score, 3),
                        "duplicate": {
                            "entity_id": duplicate["entity_id"],
                            "name": duplicate["name"],
                            "investigated_fields": investigated(duplicate),
                        },
                        "survivor": {
                            "entity_id": survivor["entity_id"],
                            "name": survivor["name"],
                            "investigated_fields": investigated(survivor),
                        },
                        "reasons": reasons,
                        "conflicting_fields": conflicts,
                    }
                )
        candidates.sort(key=lambda item: (-item["score"], item["survivor"]["name"]))
        return {"kind": kind, "candidates": candidates[:100]}

    @app.post("/api/entities/{entity_id}/merge")
    def merge_entity(entity_id: str, body: EntityMergeCreate) -> dict[str, str]:
        survivor = store().merge_entities(entity_id, body.destination, body.reason, body.actor)
        return {"entity_id": survivor, "status": "merged"}

    @app.post("/api/entities/{entity_id}/retire")
    def retire_entity(entity_id: str, body: EntityVisibilityCreate) -> dict[str, str]:
        resolved = store().set_entity_visibility(entity_id, False, body.reason, body.actor)
        return {"entity_id": resolved, "status": "retired"}

    @app.post("/api/entities/{entity_id}/restore")
    def restore_entity(entity_id: str, body: EntityVisibilityCreate) -> dict[str, str]:
        resolved = store().set_entity_visibility(entity_id, True, body.reason, body.actor)
        return {"entity_id": resolved, "status": "active"}

    @app.post("/api/entity-kinds", status_code=201)
    def add_entity_kind(body: EntityKindCreate) -> dict[str, str]:
        return {"kind": store().add_entity_kind(body.kind, body.actor)}

    @app.post("/api/questions", status_code=201)
    def add_question(body: QuestionCreate) -> dict[str, str]:
        question_id = store().add_question(
            body.name, body.subject_kind, body.value_type, body.definition, body.actor
        )
        return {"question_id": question_id}

    @app.post("/api/questions/{question_id}/policy", status_code=201)
    def update_question_policy(question_id: str, body: QuestionPolicyCreate) -> dict[str, str]:
        project = store()
        with project.connect() as connection:
            question = connection.execute(
                "SELECT * FROM questions WHERE question_id=?", (question_id,)
            ).fetchone()
            if question is None:
                raise EpiqError("question_not_found", f"Question not found: {question_id}")
            definition = json.loads(question["definition_json"])
        definition.update({"volatility": body.volatility, "freshness_days": body.freshness_days})
        next_id = project.add_question(
            str(question["name"]),
            str(question["subject_kind"]),
            str(question["value_type"]),
            definition,
            body.actor,
        )
        return {"question_id": next_id}

    def preview_question_revision(question_id: str, body: QuestionRevisionCreate) -> dict[str, Any]:
        project = store()
        project._check_type_declaration(body.value_type)
        with project.connect() as connection:
            question = project._resolve_question(connection, question_id)
        projection = project.matrix(str(question["subject_kind"]))
        incompatible = []
        checked = 0
        for row in projection["rows"]:
            cell = row["cells"][str(question["name"])]
            for value in cell.get("values", []):
                checked += 1
                try:
                    project._check_value_type(body.value_type, value)
                except EpiqError as error:
                    incompatible.append(
                        {
                            "entity_id": row["entity_id"],
                            "entity_name": row["name"],
                            "value": value,
                            "error": error.message,
                        }
                    )
        return {
            "question_id": str(question["question_id"]),
            "name": str(question["name"]),
            "current_value_type": str(question["value_type"]),
            "proposed_value_type": body.value_type,
            "checked_values": checked,
            "compatible_values": checked - len(incompatible),
            "incompatible_values": incompatible,
            "can_apply": not incompatible,
        }

    @app.post("/api/questions/{question_id}/revision-preview")
    def question_revision_preview(question_id: str, body: QuestionRevisionCreate) -> dict[str, Any]:
        return preview_question_revision(question_id, body)

    @app.post("/api/questions/{question_id}/revise", status_code=201)
    def revise_question(question_id: str, body: QuestionRevisionCreate) -> dict[str, Any]:
        preview = preview_question_revision(question_id, body)
        if not preview["can_apply"]:
            raise EpiqError(
                "incompatible_schema_revision",
                f"{len(preview['incompatible_values'])} current value(s) do not match "
                f"{body.value_type}; retract or supersede them before applying this revision",
            )
        successor = store().evolve_question(
            question_id,
            [
                {
                    "name": preview["name"],
                    "value_type": body.value_type,
                    "definition": body.definition,
                }
            ],
            "refines",
            body.reason,
            body.actor,
        )[0]
        return {"question_id": successor, "preview": preview}

    @app.post("/api/questions/{question_id}/retire")
    def retire_question(question_id: str, body: QuestionVisibilityCreate) -> dict[str, str]:
        resolved = store().set_question_visibility(question_id, False, body.reason, body.actor)
        return {"question_id": resolved, "status": "retired"}

    @app.post("/api/questions/{question_id}/restore")
    def restore_question(question_id: str, body: QuestionVisibilityCreate) -> dict[str, str]:
        resolved = store().set_question_visibility(question_id, True, body.reason, body.actor)
        return {"question_id": resolved, "status": "active"}

    @app.post("/api/questions/{question_id}/evolve", status_code=201)
    def evolve_question(question_id: str, body: QuestionEvolutionCreate) -> dict[str, Any]:
        replacements = [item.model_dump(exclude_none=True) for item in body.replacements]
        successors = store().evolve_question(
            question_id,
            replacements,
            body.relationship,
            body.reason,
            body.actor,
            body.retire_predecessor,
        )
        return {"successor_question_ids": successors}

    @app.get("/api/questions/{question_id}/lineage")
    def question_lineage(question_id: str) -> dict[str, Any]:
        return store().question_lineage(question_id)

    @app.post("/api/questions/{question_id}/challenges", status_code=201)
    def challenge_question(question_id: str, body: QuestionChallengeCreate) -> dict[str, str]:
        challenge_id = store().challenge_question(
            question_id,
            body.problem,
            body.explanation,
            body.actor,
            body.example_entity,
            body.evidence_ids,
            body.proposed_replacement,
        )
        return {"challenge_id": challenge_id, "status": "open"}

    @app.get("/api/question-challenges")
    def question_challenges(
        question: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        return store().question_challenges(question, status)

    @app.post("/api/question-challenges/{challenge_id}/resolve")
    def resolve_question_challenge(
        challenge_id: str, body: QuestionChallengeResolve
    ) -> dict[str, str]:
        store().resolve_question_challenge(challenge_id, body.status, body.resolution, body.actor)
        return {"challenge_id": challenge_id, "status": body.status}

    @app.post("/api/evidence", status_code=201)
    def add_evidence(body: EvidenceCreate) -> dict[str, str]:
        if body.source_type == "web" and not body.url:
            raise EpiqError("source_url_required", "A web source requires a URL")
        source_locator = body.url or f"urn:epiq:{body.source_type}"
        source_id, evidence_id = store().add_evidence(
            source_locator,
            body.title,
            body.retrieved_at,
            body.excerpt,
            body.actor,
            body.published_at,
            body.source_type,
        )
        return {"source_id": source_id, "evidence_id": evidence_id}

    @app.post("/api/evidence/{evidence_id}/assess", status_code=201)
    def assess_evidence(evidence_id: str, body: EvidenceAssessmentCreate) -> dict[str, str]:
        assessment_id = store().assess_evidence(evidence_id, body.status, body.reason, body.actor)
        return {"assessment_id": assessment_id, "status": body.status}

    @app.get("/api/evidence/{evidence_id}/assessments")
    def evidence_assessments(evidence_id: str) -> list[dict[str, Any]]:
        return store().evidence_assessments(evidence_id)

    @app.post("/api/claims", status_code=201)
    def add_claim(body: ClaimCreate) -> dict[str, str]:
        claim_id = store().assert_claim(
            body.subject,
            body.question,
            body.value,
            body.valid_from,
            body.evidence_ids,
            body.actor,
            confidence=body.confidence,
            temporal_basis=body.temporal_basis,
        )
        return {"claim_id": claim_id}

    @app.post("/api/claims/bulk", status_code=201)
    def add_claims_bulk(body: ClaimBatchCreate) -> dict[str, Any]:
        items = [item.model_dump(exclude={"actor"}) for item in body.claims]
        claim_ids = store().assert_claims_bulk(items, body.actor)
        return {"count": len(claim_ids), "claim_ids": claim_ids}

    @app.post("/api/batch", status_code=201)
    def write_batch(body: WriteBatchCreate) -> dict[str, Any]:
        results = store().write_batch(body.operations, body.actor)
        return {"count": len(results), "results": results}

    @app.post("/api/derive", status_code=201)
    def derive(body: DeriveCreate) -> dict[str, Any]:
        project_store = store()
        input_claims = list(body.input_claim_ids)
        for subject, question in body.input_cells:
            input_claims.extend(project_store.active_claim_ids(subject, question))
        weight_claims = []
        for subject, question in body.weight_cells:
            resolved = project_store.active_claim_ids(subject, question)
            if len(resolved) != 1:
                raise EpiqError(
                    "ambiguous_weight_cell",
                    f"Weight cell {subject} / {question} must have exactly one active claim",
                )
            weight_claims.extend(resolved)
        claim_id = project_store.derive_claim(
            body.subject,
            body.question,
            body.operation,
            input_claims,
            body.valid_from,
            body.actor,
            body.parameters,
            body.confidence,
            weight_claims,
        )
        return {
            "ok": True,
            "claim_id": claim_id,
            "operation": body.operation,
            "input_claim_ids": input_claims,
            "parameter_claim_ids": weight_claims,
        }

    @app.post("/api/derive-distribution", status_code=201)
    def derive_distribution(body: DistributionDeriveCreate) -> dict[str, Any]:
        claim_id = store().derive_distribution(
            body.subject,
            body.question,
            body.input_claim_ids,
            body.valid_from,
            body.actor,
            body.weights,
            body.confidence,
        )
        return {
            "ok": True,
            "claim_id": claim_id,
            "input_claim_ids": body.input_claim_ids,
        }

    @app.post("/api/materialize", status_code=201)
    def materialize(body: MaterializeCreate) -> dict[str, Any]:
        return store().materialize_formulas(
            body.entity_kind,
            body.valid_from,
            body.actor,
            body.subjects,
            [body.question] if body.question else None,
        )

    @app.post("/api/propagate", status_code=201)
    def propagate(body: PropagateCreate) -> dict[str, Any]:
        claim_id, source = store().propagate_claim(
            body.subject,
            body.via,
            body.source_question,
            body.target_question,
            body.direction,
            body.depth,
            body.valid_from,
            body.actor,
            body.confidence,
        )
        return {"ok": True, "claim_id": claim_id, "source_entity": source}

    @app.post("/api/claim-proposals", status_code=201)
    def propose_claim(body: ClaimProposalCreate) -> dict[str, str]:
        proposal_id = store().propose_claim(
            body.subject,
            body.question,
            body.value,
            body.valid_from,
            body.evidence_ids,
            body.actor,
            body.confidence,
            body.temporal_basis,
            body.rationale,
        )
        return {"proposal_id": proposal_id, "status": "pending"}

    @app.get("/api/claim-proposals")
    def claim_proposals(status: str | None = "pending") -> list[dict[str, Any]]:
        return store().claim_proposals(status)

    @app.post("/api/claim-proposals/review")
    def review_claim_proposals(body: ClaimProposalReview) -> dict[str, Any]:
        results = store().review_claim_proposals(
            body.proposal_ids, body.decision, body.reason, body.actor
        )
        return {"count": len(results), "results": results}

    @app.post("/api/claims/{claim_id}/retract")
    def retract(claim_id: str, body: RetractionCreate) -> dict[str, str]:
        store().close_claim(claim_id, "retracted", body.reason, body.actor)
        return {"claim_id": claim_id, "status": "retracted"}

    @app.post("/api/claims/{claim_id}/validity-end", status_code=201)
    def end_claim_validity(claim_id: str, body: ValidityEndCreate) -> dict[str, str]:
        store().end_claim_validity(claim_id, body.valid_to, body.reason, body.actor)
        return {"claim_id": claim_id, "valid_to": body.valid_to}

    @app.post("/api/claims/{claim_id}/supersede", status_code=201)
    def supersede(claim_id: str, body: SupersedeCreate) -> dict[str, str]:
        replacement_id = store().supersede_claim(
            claim_id,
            body.value,
            body.valid_from,
            body.evidence_ids,
            body.reason,
            body.actor,
            body.confidence,
            body.temporal_basis,
        )
        return {
            "claim_id": claim_id,
            "status": "superseded",
            "replacement_claim_id": replacement_id,
        }

    @app.post("/api/claims/{claim_id}/challenge", status_code=201)
    def challenge_claim(claim_id: str, body: ClaimChallengeCreate) -> dict[str, Any]:
        project = store()
        with project.connect() as connection:
            claim = connection.execute(
                "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if claim is None:
                raise EpiqError("claim_not_found", f"Claim not found: {claim_id}")
            question = connection.execute(
                "SELECT * FROM questions WHERE question_id=?", (claim["question_id"],)
            ).fetchone()
            definition = json.loads(question["definition_json"])
        project.record_claim_feedback(claim_id, body.reason, body.research_guidance, body.actor)
        if body.retract:
            project.close_claim(claim_id, "retracted", body.reason, body.actor)
        next_question_id = str(question["question_id"])
        if body.research_guidance.strip():
            notes = list(definition.get("research_guidance_notes", []))
            notes.append(body.research_guidance.strip())
            definition["research_guidance_notes"] = notes
            definition["research_guidance"] = "\n".join(notes)
            next_question_id = project.add_question(
                str(question["name"]),
                str(question["subject_kind"]),
                str(question["value_type"]),
                definition,
                body.actor,
            )
        return {
            "claim_id": claim_id,
            "status": "retracted" if body.retract else "challenged",
            "question_id": next_question_id,
            "subject_id": str(claim["subject_id"]),
        }

    @app.post("/api/research/not-found", status_code=201)
    def not_found(body: NotFoundCreate) -> dict[str, str]:
        task_id = store().record_not_found(
            body.subject, body.question, body.query, body.notes, body.actor
        )
        return {"task_id": task_id, "state": "NotFound"}

    @app.post("/api/research/{task_id}/feedback", status_code=201)
    def research_feedback(task_id: str, body: ResearchFeedbackCreate) -> dict[str, str]:
        project = store()
        result = project.record_research_feedback(
            task_id, body.reason, body.research_guidance, body.actor
        )
        next_question_id = result["question_id"]
        if body.save_to_field and body.research_guidance.strip():
            with project.connect() as connection:
                question = connection.execute(
                    "SELECT * FROM questions WHERE question_id=?", (result["question_id"],)
                ).fetchone()
                definition = json.loads(question["definition_json"])
            notes = list(definition.get("research_guidance_notes", []))
            notes.append(body.research_guidance.strip())
            definition["research_guidance_notes"] = notes
            definition["research_guidance"] = "\n".join(notes)
            next_question_id = project.add_question(
                str(question["name"]),
                str(question["subject_kind"]),
                str(question["value_type"]),
                definition,
                body.actor,
            )
        return {**result, "question_id": next_question_id, "status": "challenged"}

    @app.get("/api/history")
    def history(event_type: str | None = None) -> list[dict[str, Any]]:
        events = store().history()
        return [
            event for event in events if event_type is None or event["event_type"] == event_type
        ]

    def execute_research(job_id: str, body: ResearchCreate) -> None:
        jobs: dict[str, dict[str, Any]] = app.state.research_jobs
        with app.state.research_lock:
            if jobs[job_id].get("cancel_requested"):
                jobs[job_id]["status"] = "cancelled"
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)
                return
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = datetime.now(UTC).isoformat()
            persist_job(job_id)

        def progress(message: str) -> None:
            with app.state.research_lock:
                jobs[job_id]["messages"].append(
                    {"at": datetime.now(UTC).isoformat(), "message": message}
                )
                persist_job(job_id)

        def cancelled() -> bool:
            with app.state.research_lock:
                return bool(jobs[job_id].get("cancel_requested"))

        def finish_cancelled() -> None:
            with app.state.research_lock:
                jobs[job_id]["status"] = "cancelled"
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                jobs[job_id]["messages"].append(
                    {"at": datetime.now(UTC).isoformat(), "message": "Research cancelled"}
                )
                persist_job(job_id)

        try:
            project = store()
            projection = project.matrix(body.entity_kind)
            question = next(
                (item for item in projection["questions"] if item["question_id"] == body.question),
                None,
            )
            if question is None:
                raise EpiqError("question_not_found", f"Question not found: {body.question}")
            targets = []
            requested_ids = set(body.entity_ids or [])
            for row in projection["rows"]:
                if requested_ids and row["entity_id"] not in requested_ids:
                    continue
                cell = row["cells"][question["name"]]
                if body.mode == "fill_missing" and cell["state"] == "Unasked":
                    targets.append(
                        {
                            "entity_id": row["entity_id"],
                            "name": row["name"],
                            "aliases": row.get("aliases", []),
                            "attributes": row.get("attributes", {}),
                        }
                    )
                elif body.mode == "retry_not_found" and cell["state"] == "NotFound":
                    targets.append(
                        {
                            "entity_id": row["entity_id"],
                            "name": row["name"],
                            "aliases": row.get("aliases", []),
                            "attributes": row.get("attributes", {}),
                        }
                    )
                elif (
                    body.mode == "add_evidence" and cell["state"] == "Answered" and cell["lineage"]
                ):
                    first = cell["lineage"][0]
                    existing_evidence = [
                        {
                            "evidence_id": item["evidence_id"],
                            "source_url": item["source"]["url"],
                            "source_title": item["source"]["title"],
                            "excerpt": item["excerpt"],
                        }
                        for item in cell["lineage"]
                    ]
                    targets.append(
                        {
                            "entity_id": row["entity_id"],
                            "name": row["name"],
                            "aliases": row.get("aliases", []),
                            "attributes": row.get("attributes", {}),
                            "existing_value": (
                                cell.get("values")
                                if question["definition"].get("cardinality", "one") == "many"
                                else cell.get("value")
                            ),
                            "existing_valid_from": first.get("as_of"),
                            "claim_id": first["claim_id"],
                            "primary_evidence_id": first["evidence_id"],
                            "existing_evidence": existing_evidence,
                            "existing_evidence_ids": list(
                                dict.fromkeys(str(item["evidence_id"]) for item in cell["lineage"])
                            ),
                            "existing_source_urls": list(
                                dict.fromkeys(
                                    str(item["source"]["url"]) for item in cell["lineage"]
                                )
                            ),
                        }
                    )
            with app.state.research_lock:
                jobs[job_id]["total"] = len(targets)
                jobs[job_id]["target_entity_ids"] = [item["entity_id"] for item in targets]
                persist_job(job_id)
            research_question = {
                **question,
                "task_mode": body.mode,
                "instructions": body.instructions,
                "research_context": project.research_context(body.entity_kind),
            }
            progress(
                f"Prepared {len(targets)} {body.entity_kind} row{'s' if len(targets) != 1 else ''}"
            )
            progress("Waiting for an available research slot")
            with app.state.research_semaphore:
                findings = app.state.research_runner(
                    body.entity_kind, research_question, targets, progress
                )
            if cancelled():
                finish_cancelled()
                return
            target_ids = {item["entity_id"] for item in targets}
            targets_by_id = {item["entity_id"]: item for item in targets}
            value_type = str(question["value_type"])
            relationship_target = (
                value_type[4:-1]
                if value_type.startswith("Ref[") and value_type.endswith("]")
                else None
            )
            if relationship_target:
                related_rows = project.matrix(relationship_target)["rows"]
                related_by_name = {
                    str(name).strip().casefold(): row
                    for row in related_rows
                    for name in [row["name"], *row.get("aliases", [])]
                }
                proposals = []
                cardinality_mismatch = False
                for finding in findings:
                    entity_id = str(finding["entity_id"])
                    if entity_id not in target_ids or finding["status"] != "answered":
                        continue
                    values = finding["value"]
                    names = values if isinstance(values, list) else [values]
                    if question["definition"].get("cardinality", "one") == "one" and len(names) > 1:
                        cardinality_mismatch = True
                    for raw_name in names:
                        proposed_fields = {}
                        if isinstance(raw_name, dict):
                            name = str(raw_name.get("name") or "").strip()
                            proposed_fields = {
                                str(key): value for key, value in raw_name.items() if key != "name"
                            }
                        else:
                            name = str(raw_name).strip()
                        if not name:
                            continue
                        existing = related_by_name.get(name.casefold())
                        proposals.append(
                            {
                                "suggestion_id": f"rel_{uuid.uuid4().hex[:16]}",
                                "subject_entity_id": entity_id,
                                "subject_name": targets_by_id[entity_id]["name"],
                                "question_id": question["question_id"],
                                "question_name": question["name"],
                                "target_kind": relationship_target,
                                "target_name": name,
                                "target_entity_id": (
                                    str(existing["entity_id"]) if existing else None
                                ),
                                "action": "link" if existing else "create_and_link",
                                "proposed_fields": proposed_fields,
                                "source_type": str(finding.get("source_type") or "web"),
                                "source_url": finding.get("source_url"),
                                "source_title": str(finding.get("source_title") or "Source"),
                                "source_published_at": finding.get("source_published_at"),
                                "observed_as_of": finding.get("observed_as_of"),
                                "excerpt": str(finding.get("excerpt") or ""),
                                "confidence": str(finding.get("confidence") or "medium"),
                                "status": "pending",
                            }
                        )
                with app.state.research_lock:
                    jobs[job_id]["relationship_suggestions"] = proposals
                    if cardinality_mismatch:
                        jobs[job_id]["schema_adaptation"] = {
                            "kind": "cardinality_mismatch",
                            "question_id": question["question_id"],
                            "question_name": question["name"],
                            "label": question["definition"].get("label", question["name"]),
                            "current_cardinality": "one",
                            "proposed_cardinality": "many",
                            "status": "pending",
                        }
                    jobs[job_id]["total"] = len(proposals)
                    jobs[job_id]["completed"] = len(proposals)
                    jobs[job_id]["status"] = "completed"
                    jobs[job_id]["outcome"] = (
                        "schema_proposal"
                        if cardinality_mismatch
                        else "proposals"
                        if proposals
                        else "no_change"
                    )
                    jobs[job_id]["no_result"] = 0 if proposals else len(targets)
                    jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                    persist_job(job_id)
                progress(
                    f"Prepared {len(proposals)} relationship proposal"
                    f"{'s' if len(proposals) != 1 else ''} for review"
                )
                return
            for finding in findings:
                if cancelled():
                    finish_cancelled()
                    return
                entity_id = str(finding["entity_id"])
                if entity_id not in target_ids:
                    continue
                result_message = f"Finished research for {targets_by_id[entity_id]['name']}"
                if finding["status"] == "not_found" and body.mode in {
                    "fill_missing",
                    "retry_not_found",
                }:
                    project.record_not_found(
                        entity_id,
                        question["question_id"],
                        f"Agent research for {question['name']}",
                        str(finding.get("notes") or "No sufficient evidence found."),
                        "agent:codex",
                    )
                    with app.state.research_lock:
                        jobs[job_id]["no_result"] += 1
                        jobs[job_id]["written"] += 1
                    result_message = (
                        f"No sufficient evidence found for {targets_by_id[entity_id]['name']}"
                    )
                elif finding["status"] == "not_found" and body.mode == "add_evidence":
                    with app.state.research_lock:
                        jobs[job_id]["no_result"] += 1
                    result_message = (
                        "No additional independent source found for "
                        f"{targets_by_id[entity_id]['name']}"
                    )
                    if finding.get("notes"):
                        result_message += f": {str(finding['notes']).strip()[:500]}"
                elif finding["status"] == "answered":
                    value = finding["value"]
                    cardinality = question["definition"].get("cardinality", "one")
                    values = value if cardinality == "many" and isinstance(value, list) else [value]
                    if not values:
                        with app.state.research_lock:
                            jobs[job_id]["no_result"] += 1
                            jobs[job_id]["completed"] += 1
                            persist_job(job_id)
                        progress(
                            f"No supported values found for {targets_by_id[entity_id]['name']}"
                        )
                        continue
                    for candidate in values:
                        Store._check_value_type(value_type, candidate)
                    source_type = str(finding.get("source_type") or "web")
                    source_url = finding.get("source_url") or f"urn:epiq:{source_type}"
                    if body.mode == "add_evidence":
                        target = targets_by_id[entity_id]
                        normalized_url = str(source_url).rstrip("/").casefold()
                        existing_urls = {
                            str(url).rstrip("/").casefold()
                            for url in target["existing_source_urls"]
                        }
                        if normalized_url in existing_urls:
                            with app.state.research_lock:
                                jobs[job_id]["completed"] += 1
                                jobs[job_id]["rejected"] += 1
                                persist_job(job_id)
                            progress(
                                f"Rejected duplicate source for {target['name']}: {source_url}"
                            )
                            continue
                    _, evidence_id = project.add_evidence(
                        str(source_url),
                        str(finding["source_title"]),
                        datetime.now(UTC).date().isoformat(),
                        str(finding["excerpt"]),
                        "agent:codex",
                        finding.get("source_published_at"),
                        source_type,
                    )
                    evidence: str | list[str] = evidence_id
                    if body.mode == "add_evidence":
                        target = targets_by_id[entity_id]
                        if value == target["existing_value"]:
                            evidence = [*target["existing_evidence_ids"], evidence_id]
                        else:
                            result_message = (
                                f"Found a conflicting value for {target['name']}: "
                                f"existing {target['existing_value']!r}, new source {value!r}"
                            )
                    valid_from = str(
                        (
                            target.get("existing_valid_from")
                            if body.mode == "add_evidence" and value == target["existing_value"]
                            else finding.get("observed_as_of") or finding.get("source_published_at")
                        )
                        or datetime.now(UTC).date().isoformat()
                    )
                    for candidate in values:
                        project.assert_claim(
                            entity_id,
                            question["question_id"],
                            candidate,
                            valid_from,
                            evidence,
                            "agent:codex",
                            confidence=str(finding["confidence"]),
                            temporal_basis=(
                                "observed"
                                if finding.get("observed_as_of")
                                else "source"
                                if finding.get("source_published_at")
                                else "unknown"
                            ),
                        )
                    with app.state.research_lock:
                        jobs[job_id]["written"] += 1
                with app.state.research_lock:
                    jobs[job_id]["completed"] += 1
                    persist_job(job_id)
                progress(result_message)
            with app.state.research_lock:
                jobs[job_id]["status"] = "completed"
                jobs[job_id]["outcome"] = "changed" if jobs[job_id]["written"] else "no_change"
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)
        except Exception as error:
            with app.state.research_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(error)
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)

    @app.post("/api/research/jobs", status_code=202)
    def launch_research(body: ResearchCreate) -> dict[str, Any]:
        projection = store().matrix(body.entity_kind)
        if not any(item["question_id"] == body.question for item in projection["questions"]):
            raise EpiqError("question_not_found", f"Question not found: {body.question}")
        requested_ids = sorted(set(body.entity_ids or []))
        with app.state.research_lock:
            existing = next(
                (
                    job
                    for job in app.state.research_jobs.values()
                    if job.get("status") in {"queued", "running"}
                    and job.get("entity_kind") == body.entity_kind
                    and job.get("question_id") == body.question
                    and job.get("mode") == body.mode
                    and sorted(set(job.get("requested_entity_ids") or [])) == requested_ids
                ),
                None,
            )
            if existing is not None:
                return {**existing, "deduplicated": True}
            job_id = f"job_{uuid.uuid4().hex[:16]}"
            job = {
                "job_id": job_id,
                "job_type": "research",
                "entity_kind": body.entity_kind,
                "question_id": body.question,
                "mode": body.mode,
                "instructions": body.instructions,
                "requested_entity_ids": requested_ids or None,
                "scope": body.scope,
                "status": "queued",
                "total": 0,
                "completed": 0,
                "target_entity_ids": [],
                "created_at": datetime.now(UTC).isoformat(),
                "error": None,
                "cancel_requested": False,
                "written": 0,
                "no_result": 0,
                "rejected": 0,
                "outcome": None,
                "relationship_suggestions": [],
                "schema_adaptation": None,
                "messages": [
                    {"at": datetime.now(UTC).isoformat(), "message": "Research job queued"}
                ],
                "deduplicated": False,
            }
            app.state.research_jobs[job_id] = job
            persist_job(job_id)
        threading.Thread(target=execute_research, args=(job_id, body), daemon=True).start()
        return job

    @app.post("/api/research/column", status_code=202)
    def launch_column_research(body: ResearchCreate) -> dict[str, Any]:
        """Fan a column request into cell jobs so results can arrive independently."""
        projection = store().matrix(body.entity_kind)
        question = next(
            (item for item in projection["questions"] if item["question_id"] == body.question),
            None,
        )
        if question is None:
            raise EpiqError("question_not_found", f"Question not found: {body.question}")
        entity_ids = []
        for row in projection["rows"]:
            cell = row["cells"][question["name"]]
            if body.mode == "fill_missing" and cell["state"] == "Unasked":
                entity_ids.append(str(row["entity_id"]))
            elif body.mode == "add_evidence" and cell["state"] == "Answered":
                entity_ids.append(str(row["entity_id"]))
        jobs = [
            launch_research(
                ResearchCreate(
                    entity_kind=body.entity_kind,
                    question=body.question,
                    mode=body.mode,
                    instructions=body.instructions,
                    entity_ids=[entity_id],
                    scope=body.scope,
                )
            )
            for entity_id in entity_ids
        ]
        return {"question_id": body.question, "cells": len(jobs), "jobs": jobs}

    def execute_workspace_agent(job_id: str, body: WorkspaceAgentCreate) -> None:
        jobs: dict[str, dict[str, Any]] = app.state.research_jobs

        def progress(message: str) -> None:
            with app.state.research_lock:
                jobs[job_id]["messages"].append(
                    {"at": datetime.now(UTC).isoformat(), "message": message}
                )
                persist_job(job_id)

        with app.state.research_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = datetime.now(UTC).isoformat()
            persist_job(job_id)
        try:
            project = store()
            overview = project.overview()
            context = {
                "project": overview["project"],
                "available_operations": agent_operation_catalog(),
                "tables": [
                    {
                        "kind": item["kind"],
                        "rows": [row["name"] for row in project.matrix(str(item["kind"]))["rows"]],
                        "questions": [
                            {
                                "name": question["name"],
                                "value_type": question["value_type"],
                                "definition": question["definition"],
                            }
                            for question in project.matrix(str(item["kind"]))["questions"]
                        ],
                    }
                    for item in overview["entity_kinds"]
                ],
            }
            progress("Planning tables, fields, rows, and research tasks")
            plan = jobs[job_id].get("workspace_plan")
            if plan is None:
                plan = app.state.workspace_agent_runner(body.message, context, progress)
                estimated_cells = sum(len(item["entity_names"]) for item in plan["research"])
                progress(
                    f"Plan ready for review: {len(plan['entity_kinds'])} tables, "
                    f"{len(plan['questions'])} fields, {estimated_cells} research cells"
                )
                with app.state.research_lock:
                    jobs[job_id]["status"] = "completed"
                    jobs[job_id]["outcome"] = "workspace_proposal"
                    jobs[job_id]["assistant_summary"] = str(plan["summary"]).strip()
                    jobs[job_id]["workspace_plan"] = plan
                    jobs[job_id]["approval_status"] = "pending"
                    jobs[job_id]["estimated_research_cells"] = estimated_cells
                    jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                    persist_job(job_id)
                return
            progress("Applying the approved workspace plan")
            entity_kinds = list(dict.fromkeys(str(item).strip() for item in plan["entity_kinds"]))
            entities = list(plan["entities"])
            questions = list(plan["questions"])
            research = list(plan["research"])
            if len(entity_kinds) > 6 or len(entities) > 15 or len(questions) > 24:
                raise EpiqError(
                    "workspace_plan_too_large",
                    "Workspace agent exceeded the bounded schema plan",
                )
            if jobs[job_id].get("cancel_requested"):
                raise EpiqError("workspace_cancelled", "Workspace agent was cancelled")

            existing_kinds = {str(item["kind"]) for item in overview["entity_kinds"]}
            current_entities = {
                (str(item["kind"]), str(row["name"]).casefold())
                for item in overview["entity_kinds"]
                for row in project.matrix(str(item["kind"]))["rows"]
            }
            current_questions = {
                (str(item["kind"]), str(question["name"]))
                for item in overview["entity_kinds"]
                for question in project.matrix(str(item["kind"]))["questions"]
            }
            normalized_entities = []
            for item in entities:
                kind, name = str(item["kind"]).strip(), str(item["name"]).strip()
                if not kind or not name or (kind, name.casefold()) in current_entities:
                    continue
                normalized_entities.append({"kind": kind, "name": name, "attributes": {}})
                current_entities.add((kind, name.casefold()))
                entity_kinds.append(kind)
            normalized_questions = []
            for item in questions:
                kind, name = str(item["kind"]).strip(), str(item["name"]).strip()
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
                    raise EpiqError("invalid_question_name", f"Invalid question name: {name}")
                if (kind, name) in current_questions:
                    continue
                value_type = str(item["value_type"]).strip()
                Store._check_type_declaration(value_type)
                normalized_questions.append(
                    {
                        "name": name,
                        "subject_kind": kind,
                        "value_type": value_type,
                        "definition": {
                            "label": str(item["label"]).strip() or name.replace("_", " ").title(),
                            "cardinality": str(item["cardinality"]),
                            "volatility": str(item["volatility"]),
                            "freshness_days": item["freshness_days"],
                            "research_guidance": str(item["research_guidance"]).strip(),
                        },
                    }
                )
                current_questions.add((kind, name))
                entity_kinds.append(kind)
            entity_kinds = list(dict.fromkeys([*existing_kinds, *entity_kinds]))
            progress(
                f"Applying {len(normalized_entities)} rows and {len(normalized_questions)} fields"
            )
            applied = project.apply_document(
                {
                    "entity_kinds": entity_kinds,
                    "entities": normalized_entities,
                    "questions": normalized_questions,
                },
                "agent:workspace",
            )

            projections = {
                str(item["kind"]): project.matrix(str(item["kind"]))
                for item in project.overview()["entity_kinds"]
            }
            normalized_research = []
            skipped_research = 0
            for request in research:
                requested_kind = str(request["kind"]).strip()
                requested_names = {
                    str(name).strip().casefold()
                    for name in request["entity_names"]
                    if str(name).strip()
                }
                kind = requested_kind
                if kind not in projections:
                    candidate_kinds = [
                        candidate_kind
                        for candidate_kind, candidate_projection in projections.items()
                        if requested_names
                        and requested_names
                        <= {str(row["name"]).casefold() for row in candidate_projection["rows"]}
                    ]
                    if len(candidate_kinds) != 1:
                        skipped_research += 1
                        progress(
                            f"Could not resolve research table {requested_kind!r} "
                            "from its requested rows"
                        )
                        continue
                    kind = candidate_kinds[0]
                    progress(f"Resolved research table {requested_kind!r} to {kind}")
                projection = projections[kind]
                available_questions = {
                    str(question["name"]): question for question in projection["questions"]
                }
                requested_question = str(request["question"]).strip()
                selected_questions = []
                if requested_question in available_questions:
                    selected_questions = [requested_question]
                else:
                    task_text = f"{requested_question}\n{request['instructions']}"
                    selected_questions = [
                        name
                        for name in available_questions
                        if re.search(
                            rf"(?<![a-zA-Z0-9_]){re.escape(name)}(?![a-zA-Z0-9_])",
                            task_text,
                        )
                    ]
                if not selected_questions:
                    skipped_research += 1
                    progress(
                        f"Could not resolve research field {requested_kind}.{requested_question}"
                    )
                    continue
                if selected_questions != [requested_question]:
                    progress(
                        f"Expanded research task for {kind} into {len(selected_questions)} fields"
                    )
                normalized_research.extend(
                    {
                        **request,
                        "kind": kind,
                        "question": question_name,
                        "instructions": (
                            f"{requested_question}\n\n{request['instructions']}"
                        ).strip(),
                    }
                    for question_name in selected_questions
                )
            if research and not normalized_research:
                raise EpiqError(
                    "invalid_workspace_research_plan",
                    "The workspace agent proposed research but none of its table or field "
                    "references could be resolved",
                    "Retry the direction; research.kind must name a table and "
                    "research.question must name one field.",
                )
            if skipped_research:
                progress(f"Skipped {skipped_research} unresolvable research tasks")
            research = normalized_research
            question_types = {
                (kind, str(question["name"])): str(question["value_type"])
                for kind, projection in projections.items()
                for question in projection["questions"]
            }
            research.sort(
                key=lambda item: (
                    not question_types.get(
                        (str(item["kind"]), str(item["question"])), ""
                    ).startswith("Ref["),
                )
            )
            child_job_ids = []
            requested_cells = 0
            for request in research:
                kind = str(request["kind"])
                projection = project.matrix(kind)
                question = next(
                    (
                        item
                        for item in projection["questions"]
                        if item["name"] == str(request["question"])
                    ),
                    None,
                )
                if question is None:
                    progress(f"Skipped unknown research field {kind}.{request['question']}")
                    continue
                requested_names = {str(name).casefold() for name in request["entity_names"]}
                targets = [
                    row
                    for row in projection["rows"]
                    if not requested_names or str(row["name"]).casefold() in requested_names
                ]
                for row in targets:
                    child = launch_research(
                        ResearchCreate(
                            entity_kind=kind,
                            question=str(question["question_id"]),
                            mode="fill_missing",
                            instructions=str(request["instructions"]),
                            entity_ids=[str(row["entity_id"])],
                            scope="cell",
                        )
                    )
                    child_job_ids.append(str(child["job_id"]))
                    requested_cells += 1
            summary = str(plan["summary"]).strip()
            progress(f"Workspace ready; launched {len(child_job_ids)} cell research jobs")
            with app.state.research_lock:
                jobs[job_id]["status"] = "completed"
                jobs[job_id]["outcome"] = "changed"
                jobs[job_id]["assistant_summary"] = summary
                jobs[job_id]["workspace_plan"] = plan
                jobs[job_id]["applied"] = applied
                jobs[job_id]["child_job_ids"] = child_job_ids
                jobs[job_id]["total"] = len(normalized_entities) + len(normalized_questions)
                jobs[job_id]["completed"] = jobs[job_id]["total"]
                jobs[job_id]["written"] = jobs[job_id]["total"]
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)
        except Exception as error:
            with app.state.research_lock:
                jobs[job_id]["status"] = (
                    "cancelled"
                    if jobs[job_id].get("cancel_requested")
                    or isinstance(error, EpiqError)
                    and error.code == "workspace_cancelled"
                    else "failed"
                )
                jobs[job_id]["error"] = str(error)
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)

    @app.post("/api/workspace-agent/jobs", status_code=202)
    def launch_workspace_agent(body: WorkspaceAgentCreate) -> dict[str, Any]:
        store().overview()
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "job_type": "workspace_agent",
            "entity_kind": "Workspace",
            "question_id": "",
            "mode": "workspace_agent",
            "instructions": body.message,
            "user_message": body.message,
            "status": "queued",
            "total": 0,
            "completed": 0,
            "target_entity_ids": [],
            "created_at": datetime.now(UTC).isoformat(),
            "error": None,
            "cancel_requested": False,
            "written": 0,
            "outcome": None,
            "messages": [
                {"at": datetime.now(UTC).isoformat(), "message": "Workspace agent queued"}
            ],
        }
        with app.state.research_lock:
            app.state.research_jobs[job_id] = job
            persist_job(job_id)
        threading.Thread(target=execute_workspace_agent, args=(job_id, body), daemon=True).start()
        return job

    @app.post("/api/workspace-agent/jobs/{job_id}/approve", status_code=202)
    def approve_workspace_agent(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None or job.get("job_type") != "workspace_agent":
                raise EpiqError("workspace_job_not_found", f"Workspace job not found: {job_id}")
            if job.get("approval_status") != "pending" or not job.get("workspace_plan"):
                raise EpiqError(
                    "workspace_plan_unavailable",
                    "This workspace plan is not awaiting approval",
                )
            job["approval_status"] = "approved"
            job["status"] = "queued"
            job["outcome"] = None
            job["error"] = None
            job["messages"].append(
                {"at": datetime.now(UTC).isoformat(), "message": "Plan approved; execution queued"}
            )
            persist_job(job_id)
            body = WorkspaceAgentCreate(message=str(job.get("user_message") or job["instructions"]))
        threading.Thread(target=execute_workspace_agent, args=(job_id, body), daemon=True).start()
        return job

    @app.post("/api/workspace-agent/jobs/{job_id}/reject")
    def reject_workspace_agent(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None or job.get("job_type") != "workspace_agent":
                raise EpiqError("workspace_job_not_found", f"Workspace job not found: {job_id}")
            if job.get("approval_status") != "pending":
                raise EpiqError(
                    "workspace_plan_unavailable",
                    "This workspace plan is not awaiting approval",
                )
            job["approval_status"] = "rejected"
            job["outcome"] = "workspace_proposal"
            job["messages"].append(
                {"at": datetime.now(UTC).isoformat(), "message": "Plan dismissed without changes"}
            )
            persist_job(job_id)
            return job

    @app.get("/api/research/jobs")
    def research_jobs() -> list[dict[str, Any]]:
        with app.state.research_lock:
            return list(reversed(list(app.state.research_jobs.values())))

    @app.post("/api/research/jobs/{job_id}/cancel")
    def cancel_research(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("research_job_not_found", f"Research job not found: {job_id}")
            if job["status"] not in {"queued", "running"}:
                raise EpiqError(
                    "research_job_finished",
                    f"Research job is already {job['status']}",
                )
            job["cancel_requested"] = True
            job["messages"].append(
                {"at": datetime.now(UTC).isoformat(), "message": "Cancellation requested"}
            )
            persist_job(job_id)
            return job

    @app.post("/api/research/cancel")
    def cancel_research_scope(body: ResearchCancelScope) -> dict[str, Any]:
        if body.scope in {"cell", "row"} and not body.entity_id:
            raise EpiqError(
                "invalid_cancel_scope", f"{body.scope.title()} cancel requires entity_id"
            )
        if body.scope in {"cell", "column"} and not body.question_id:
            raise EpiqError(
                "invalid_cancel_scope", f"{body.scope.title()} cancel requires question_id"
            )
        cancelled = []
        with app.state.research_lock:
            for job_id, job in app.state.research_jobs.items():
                if job.get("status") not in {"queued", "running"}:
                    continue
                if job.get("job_type", "research") != "research":
                    continue
                if job.get("entity_kind") != body.entity_kind:
                    continue
                if body.question_id and job.get("question_id") != body.question_id:
                    continue
                entity_ids = {
                    *job.get("target_entity_ids", []),
                    *(job.get("requested_entity_ids") or []),
                }
                if body.entity_id and body.entity_id not in entity_ids:
                    continue
                if not job.get("cancel_requested"):
                    job["cancel_requested"] = True
                    job["messages"].append(
                        {
                            "at": datetime.now(UTC).isoformat(),
                            "message": f"Cancellation requested from {body.scope} scope",
                        }
                    )
                    persist_job(job_id)
                cancelled.append(dict(job))
        return {"scope": body.scope, "count": len(cancelled), "jobs": cancelled}

    @app.post("/api/research/jobs/{job_id}/retry", status_code=202)
    def retry_research(job_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("research_job_not_found", f"Research job not found: {job_id}")
            if job["status"] not in {"failed", "cancelled"}:
                raise EpiqError(
                    "research_job_not_retryable",
                    "Only failed or cancelled research jobs can be retried; "
                    f"this job is {job['status']}",
                )
            request = ResearchCreate(
                entity_kind=str(job["entity_kind"]),
                question=str(job["question_id"]),
                mode=str(job["mode"]),
                instructions=str(job.get("instructions") or ""),
                entity_ids=job.get("requested_entity_ids"),
                scope=str(job.get("scope") or "column"),
            )
        replacement = launch_research(request)
        replacement_id = str(replacement["job_id"])
        with app.state.research_lock:
            for parent_id, parent in app.state.research_jobs.items():
                child_ids = parent.get("child_job_ids")
                if not isinstance(child_ids, list) or job_id not in child_ids:
                    continue
                parent["child_job_ids"] = [
                    replacement_id if child_id == job_id else child_id
                    for child_id in child_ids
                ]
                parent.setdefault("messages", []).append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "message": (
                            f"Replaced failed child job {job_id} with retry {replacement_id}"
                        ),
                    }
                )
                persist_job(parent_id)
        return replacement

    def scoped_relationship_suggestions(
        body: RelationshipReviewScope,
    ) -> list[tuple[str, dict[str, Any]]]:
        if body.scope == "cell" and (not body.subject_entity_id or not body.question_id):
            raise EpiqError(
                "invalid_review_scope",
                "Cell review requires subject_entity_id and question_id",
            )
        if body.scope == "column" and not body.question_id:
            raise EpiqError("invalid_review_scope", "Column review requires question_id")
        selected: list[tuple[str, dict[str, Any]]] = []
        with app.state.research_lock:
            for job_id, job in app.state.research_jobs.items():
                if body.entity_kind and job.get("entity_kind") != body.entity_kind:
                    continue
                for item in job.get("relationship_suggestions", []):
                    if item.get("status") != "pending":
                        continue
                    if body.question_id and item.get("question_id") != body.question_id:
                        continue
                    if (
                        body.subject_entity_id
                        and item.get("subject_entity_id") != body.subject_entity_id
                    ):
                        continue
                    selected.append((job_id, dict(item)))
        if not selected:
            raise EpiqError("empty_review", "No pending provisional relationships match this scope")
        return selected

    def mark_relationship_suggestions_accepted(
        selected: list[tuple[str, dict[str, Any]]], result: dict[str, Any]
    ) -> None:
        accepted_by_id = {str(item["suggestion_id"]): item for item in result.get("accepted", [])}
        touched_jobs = {job_id for job_id, _ in selected}
        with app.state.research_lock:
            for job_id in touched_jobs:
                job = app.state.research_jobs[job_id]
                for suggestion in job.get("relationship_suggestions", []):
                    accepted = accepted_by_id.get(str(suggestion["suggestion_id"]))
                    if accepted:
                        suggestion.update(accepted)
                        suggestion["status"] = "accepted"
                persist_job(job_id)

    def mark_relationship_suggestions_rejected(
        selected: list[tuple[str, dict[str, Any]]], result: dict[str, Any]
    ) -> None:
        rejected_ids = {str(item["suggestion_id"]) for item in result.get("rejected", [])}
        touched_jobs = {job_id for job_id, _ in selected}
        with app.state.research_lock:
            for job_id in touched_jobs:
                job = app.state.research_jobs[job_id]
                for suggestion in job.get("relationship_suggestions", []):
                    if str(suggestion["suggestion_id"]) in rejected_ids:
                        suggestion["status"] = "dismissed"
                persist_job(job_id)

    @app.post("/api/research/relationships/preview")
    def preview_relationship_review(body: RelationshipReviewScope) -> dict[str, Any]:
        selected = scoped_relationship_suggestions(body)
        findings = [item for _, item in selected]
        return {
            "review_id": body.review_id,
            "scope": body.scope,
            "count": len(findings),
            "jobs": len({job_id for job_id, _ in selected}),
            "subjects": len({str(item["subject_entity_id"]) for item in findings}),
            "questions": len({str(item["question_id"]) for item in findings}),
            "creates": sum(1 for item in findings if not item.get("target_entity_id")),
            "links": sum(1 for item in findings if item.get("target_entity_id")),
            "suggestion_ids": [str(item["suggestion_id"]) for item in findings],
        }

    @app.post("/api/research/relationships/accept", status_code=201)
    def accept_scoped_relationship_review(body: RelationshipReviewScope) -> dict[str, Any]:
        selected = scoped_relationship_suggestions(body)
        findings = [
            {**item, "retrieved_at": datetime.now(UTC).date().isoformat()} for _, item in selected
        ]
        result = store().approve_relationship_findings(
            findings, body.actor, body.review_id, body.reason
        )
        mark_relationship_suggestions_accepted(selected, result)
        return {**result, "review_id": body.review_id, "scope": body.scope}

    @app.post("/api/research/relationships/reject", status_code=200)
    def reject_scoped_relationship_review(body: RelationshipReviewScope) -> dict[str, Any]:
        selected = scoped_relationship_suggestions(body)
        result = store().reject_relationship_findings(
            [str(item["suggestion_id"]) for _, item in selected],
            body.actor,
            body.review_id,
            body.reason,
        )
        mark_relationship_suggestions_rejected(selected, result)
        return {**result, "review_id": body.review_id, "scope": body.scope}

    @app.post("/api/research/jobs/{job_id}/relationships/accept", status_code=201)
    def accept_relationship_suggestions(
        job_id: str, body: AcceptRelationshipSuggestionsCreate
    ) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None:
                raise EpiqError("research_job_not_found", f"Research job not found: {job_id}")
            selected = [
                dict(item)
                for item in job.get("relationship_suggestions", [])
                if item["suggestion_id"] in set(body.suggestion_ids) and item["status"] == "pending"
            ]
        if len(selected) != len(set(body.suggestion_ids)):
            raise EpiqError(
                "relationship_suggestion_not_found",
                "One or more relationship suggestions are unavailable or already reviewed",
            )
        findings = [
            {**item, "retrieved_at": datetime.now(UTC).date().isoformat()} for item in selected
        ]
        suggestion_key = ",".join(sorted(str(item["suggestion_id"]) for item in selected))
        review_id = f"rrv_job_{sha256(f'{job_id}:{suggestion_key}'.encode()).hexdigest()[:20]}"
        result = store().approve_relationship_findings(
            findings,
            body.actor,
            review_id,
            "Approved selected relationship research findings",
        )
        mark_relationship_suggestions_accepted([(job_id, item) for item in selected], result)
        return result

    @app.post("/api/research/schema-adaptations/{question_id}/accept", status_code=201)
    def accept_schema_adaptation(
        question_id: str, body: SchemaAdaptationAcceptCreate
    ) -> dict[str, Any]:
        with app.state.research_lock:
            matching = [
                (job_id, job)
                for job_id, job in app.state.research_jobs.items()
                if (job.get("schema_adaptation") or {}).get("question_id") == question_id
                and (job.get("schema_adaptation") or {}).get("status") in {"pending", "applying"}
            ]
        if not matching:
            raise EpiqError(
                "schema_adaptation_not_found",
                "No pending schema adaptation was found for this field",
            )
        job_ids = [job_id for job_id, _ in matching]
        with app.state.research_lock:
            for job_id, job in matching:
                job["schema_adaptation"]["status"] = "applying"
                persist_job(job_id)
        findings = [
            {**dict(suggestion), "retrieved_at": datetime.now(UTC).date().isoformat()}
            for _, job in matching
            for suggestion in job.get("relationship_suggestions", [])
            if suggestion["status"] == "pending"
        ]
        result = store().apply_change_set(
            {
                "change_set_id": f"cs_relationship_cardinality_{question_id}",
                "kind": "relationship_cardinality",
                "predecessor_question_id": question_id,
                "reason": "Bulk-approved agent finding: observed multiple related rows",
                "findings": findings,
            },
            body.actor,
        )
        accepted_by_id = {item["suggestion_id"]: item for item in result.get("accepted", [])}
        with app.state.research_lock:
            for job_id, job in matching:
                job["question_id"] = result["question_id"]
                job["schema_adaptation"]["status"] = "applied"
                job["schema_adaptation"]["successor_question_id"] = result["question_id"]
                for suggestion in job.get("relationship_suggestions", []):
                    accepted = accepted_by_id.get(suggestion["suggestion_id"])
                    if accepted:
                        suggestion.update(accepted)
                        suggestion["question_id"] = result["question_id"]
                        suggestion["status"] = "accepted"
                persist_job(job_id)
        return {**result, "jobs": job_ids}

    @app.post("/api/research/rows", status_code=202)
    def launch_row_research(body: RowResearchCreate) -> dict[str, Any]:
        projection = store().matrix(body.entity_kind)
        row = next(
            (item for item in projection["rows"] if item["entity_id"] == body.entity_id), None
        )
        if row is None:
            raise EpiqError("entity_not_found", f"Entity not found: {body.entity_id}")
        questions = [
            question
            for question in projection["questions"]
            if row["cells"][question["name"]]["state"] == "Unasked"
        ]
        jobs = []
        for question in questions:
            jobs.append(
                launch_research(
                    ResearchCreate(
                        entity_kind=body.entity_kind,
                        question=question["question_id"],
                        mode="fill_missing",
                        instructions=body.instructions,
                        entity_ids=[body.entity_id],
                        scope="row",
                    )
                )
            )
        return {"entity_id": body.entity_id, "questions": len(questions), "jobs": jobs}

    @app.post("/api/research/table", status_code=202)
    def launch_table_research(body: TableResearchCreate) -> dict[str, Any]:
        projection = store().matrix(body.entity_kind)
        questions = [
            question
            for question in projection["questions"]
            if any(
                row["cells"][question["name"]]["state"] == "Unasked" for row in projection["rows"]
            )
        ]
        jobs = []
        for question in questions:
            result = launch_column_research(
                ResearchCreate(
                    entity_kind=body.entity_kind,
                    question=question["question_id"],
                    mode="fill_missing",
                    instructions=body.instructions,
                    scope="table",
                )
            )
            jobs.extend(result["jobs"])
        return {"questions": len(questions), "jobs": jobs}

    def execute_suggestions(job_id: str, body: SuggestEntitiesCreate) -> None:
        jobs: dict[str, dict[str, Any]] = app.state.research_jobs

        def progress(message: str) -> None:
            with app.state.research_lock:
                jobs[job_id]["messages"].append(
                    {"at": datetime.now(UTC).isoformat(), "message": message}
                )
                persist_job(job_id)

        with app.state.research_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = datetime.now(UTC).isoformat()
            persist_job(job_id)
        try:
            projection = store().matrix(body.entity_kind)
            existing = [
                {"entity_id": row["entity_id"], "name": row["name"]} for row in projection["rows"]
            ]
            progress(f"Looking for {body.count} additional {body.entity_kind} rows")
            candidates = app.state.suggestion_runner(
                body.entity_kind, existing, body.count, body.instructions, progress
            )
            existing_names = {item["name"].strip().casefold() for item in existing}
            seen = set(existing_names)
            suggestions = []
            for candidate in candidates:
                name = str(candidate.get("name", "")).strip()
                if not name or name.casefold() in seen:
                    continue
                seen.add(name.casefold())
                suggestions.append(
                    {
                        "suggestion_id": f"sug_{uuid.uuid4().hex[:16]}",
                        "name": name,
                        "rationale": str(candidate.get("rationale", "")),
                        "source_title": str(candidate.get("source_title", "")),
                        "source_url": str(candidate.get("source_url", "")),
                        "status": "pending",
                        "entity_id": None,
                    }
                )
            with app.state.research_lock:
                jobs[job_id]["suggestions"] = suggestions[: body.count]
                jobs[job_id]["total"] = len(suggestions[: body.count])
                jobs[job_id]["completed"] = len(suggestions[: body.count])
                jobs[job_id]["status"] = "completed"
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)
            progress(f"Prepared {len(suggestions[: body.count])} suggestions for review")
        except Exception as error:
            with app.state.research_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(error)
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)

    @app.post("/api/entity-suggestions/jobs", status_code=202)
    def launch_suggestions(body: SuggestEntitiesCreate) -> dict[str, Any]:
        # matrix also validates that the entity kind exists in this project.
        store().matrix(body.entity_kind)
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "job_type": "entity_suggestions",
            "entity_kind": body.entity_kind,
            "question_id": "",
            "mode": "suggest_entities",
            "instructions": body.instructions,
            "status": "queued",
            "total": 0,
            "completed": 0,
            "target_entity_ids": [],
            "created_at": datetime.now(UTC).isoformat(),
            "error": None,
            "suggestions": [],
            "messages": [{"at": datetime.now(UTC).isoformat(), "message": "Suggestion job queued"}],
        }
        with app.state.research_lock:
            app.state.research_jobs[job_id] = job
            persist_job(job_id)
        threading.Thread(target=execute_suggestions, args=(job_id, body), daemon=True).start()
        return job

    @app.post("/api/entity-suggestions/{job_id}/accept", status_code=201)
    def accept_suggestion(job_id: str, body: AcceptSuggestionCreate) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None or job.get("job_type") != "entity_suggestions":
                raise EpiqError("suggestion_job_not_found", f"Suggestion job not found: {job_id}")
            suggestion = next(
                (
                    item
                    for item in job["suggestions"]
                    if item["suggestion_id"] == body.suggestion_id
                ),
                None,
            )
            if suggestion is None:
                raise EpiqError("suggestion_not_found", "Suggestion not found in this job")
            if suggestion["status"] == "accepted":
                return suggestion
            entity_kind = str(job["entity_kind"])
            name = str(suggestion["name"])
        entity_id = store().add_entity(
            entity_kind,
            name,
            {
                "suggested_by": "agent",
                "suggestion_source_url": suggestion["source_url"],
                "suggestion_source_title": suggestion["source_title"],
            },
            body.actor,
        )
        with app.state.research_lock:
            suggestion["status"] = "accepted"
            suggestion["entity_id"] = entity_id
            persist_job(job_id)
            return dict(suggestion)

    @app.post("/api/entity-suggestions/{job_id}/{suggestion_id}/dismiss")
    def dismiss_suggestion(job_id: str, suggestion_id: str) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None or job.get("job_type") != "entity_suggestions":
                raise EpiqError("suggestion_job_not_found", f"Suggestion job not found: {job_id}")
            suggestion = next(
                (item for item in job["suggestions"] if item["suggestion_id"] == suggestion_id),
                None,
            )
            if suggestion is None:
                raise EpiqError("suggestion_not_found", "Suggestion not found in this job")
            suggestion["status"] = "dismissed"
            persist_job(job_id)
            return dict(suggestion)

    def execute_field_suggestions(job_id: str, body: SuggestFieldsCreate) -> None:
        jobs: dict[str, dict[str, Any]] = app.state.research_jobs

        def progress(message: str) -> None:
            with app.state.research_lock:
                jobs[job_id]["messages"].append(
                    {"at": datetime.now(UTC).isoformat(), "message": message}
                )
                persist_job(job_id)

        with app.state.research_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = datetime.now(UTC).isoformat()
            persist_job(job_id)
        try:
            projection = store().matrix(body.entity_kind)
            existing = [
                {
                    "name": question["name"],
                    "label": question["definition"].get("label", question["name"]),
                    "value_type": question["value_type"],
                    "definition": question["definition"],
                }
                for question in projection["questions"]
            ]
            sample_entities = [
                {"entity_id": row["entity_id"], "name": row["name"]}
                for row in projection["rows"][:10]
            ]
            progress(f"Designing {body.count} complementary fields")
            candidates = app.state.field_suggestion_runner(
                body.entity_kind,
                existing,
                sample_entities,
                body.count,
                body.instructions,
                progress,
            )
            existing_names = {item["name"].casefold() for item in existing}
            seen = set(existing_names)
            suggestions = []
            for candidate in candidates:
                name = str(candidate.get("name", "")).strip()
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", name) or name.casefold() in seen:
                    continue
                value_type = str(candidate.get("value_type", "")).strip()
                try:
                    Store._check_type_declaration(value_type)
                except EpiqError:
                    continue
                seen.add(name.casefold())
                suggestions.append(
                    {
                        "suggestion_id": f"fsug_{uuid.uuid4().hex[:16]}",
                        "name": name,
                        "label": str(candidate.get("label", "")).strip() or name,
                        "value_type": value_type,
                        "rationale": str(candidate.get("rationale", "")).strip(),
                        "research_guidance": str(candidate.get("research_guidance", "")).strip(),
                        "status": "pending",
                        "question_id": None,
                    }
                )
            selected = suggestions[: body.count]
            with app.state.research_lock:
                jobs[job_id]["field_suggestions"] = selected
                jobs[job_id]["total"] = len(selected)
                jobs[job_id]["completed"] = len(selected)
                jobs[job_id]["status"] = "completed"
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)
            progress(f"Prepared {len(selected)} field suggestions for review")
        except Exception as error:
            with app.state.research_lock:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = str(error)
                jobs[job_id]["finished_at"] = datetime.now(UTC).isoformat()
                persist_job(job_id)

    @app.post("/api/field-suggestions/jobs", status_code=202)
    def launch_field_suggestions(body: SuggestFieldsCreate) -> dict[str, Any]:
        store().matrix(body.entity_kind)
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "job_type": "field_suggestions",
            "entity_kind": body.entity_kind,
            "question_id": "",
            "mode": "suggest_fields",
            "instructions": body.instructions,
            "status": "queued",
            "total": 0,
            "completed": 0,
            "target_entity_ids": [],
            "created_at": datetime.now(UTC).isoformat(),
            "error": None,
            "field_suggestions": [],
            "messages": [
                {"at": datetime.now(UTC).isoformat(), "message": "Field suggestion job queued"}
            ],
        }
        with app.state.research_lock:
            app.state.research_jobs[job_id] = job
            persist_job(job_id)
        threading.Thread(target=execute_field_suggestions, args=(job_id, body), daemon=True).start()
        return job

    @app.post("/api/field-suggestions/{job_id}/accept", status_code=201)
    def accept_field_suggestions(job_id: str, body: AcceptFieldSuggestionsCreate) -> dict[str, Any]:
        with app.state.research_lock:
            job = app.state.research_jobs.get(job_id)
            if job is None or job.get("job_type") != "field_suggestions":
                raise EpiqError("suggestion_job_not_found", f"Suggestion job not found: {job_id}")
            by_id = {item["suggestion_id"]: item for item in job["field_suggestions"]}
            missing = [item for item in body.suggestion_ids if item not in by_id]
            if missing:
                raise EpiqError("suggestion_not_found", f"Field suggestion not found: {missing[0]}")
            selected = [by_id[item] for item in body.suggestion_ids]
            entity_kind = str(job["entity_kind"])
        accepted = []
        for suggestion in selected:
            if suggestion["status"] == "accepted":
                accepted.append(dict(suggestion))
                continue
            question_id = store().add_question(
                suggestion["name"],
                entity_kind,
                suggestion["value_type"],
                {
                    "label": suggestion["label"],
                    "research_guidance": suggestion["research_guidance"],
                    "suggested_by": "agent",
                    "suggestion_rationale": suggestion["rationale"],
                },
                body.actor,
            )
            with app.state.research_lock:
                suggestion["status"] = "accepted"
                suggestion["question_id"] = question_id
                accepted.append(dict(suggestion))
                persist_job(job_id)
        return {"accepted": accepted}

    if frontend_path.exists():
        assets = frontend_path / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            candidate = frontend_path / path
            return FileResponse(candidate if candidate.is_file() else frontend_path / "index.html")

    return app


def main() -> None:
    """Run the local web application."""
    import uvicorn

    environment = Path(".env")
    if environment.exists():
        for line in environment.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=int(os.environ.get("EPIQ_PORT", "8000")),
        access_log=False,
    )
