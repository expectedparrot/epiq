"""FastAPI application exposing an Epiq database to the spreadsheet UI."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime
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

from .errors import EpiqError
from .research import (
    EntitySuggestionRunner,
    FieldSuggestionRunner,
    OpenAIEntitySuggestionRunner,
    OpenAIFieldSuggestionRunner,
    OpenAIResearchRunner,
    ResearchRunner,
)
from .store import Store
from .xlsx import write_xlsx


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)


class ProjectOpen(BaseModel):
    project_id: str


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


class SuggestEntitiesCreate(BaseModel):
    entity_kind: str = Field(min_length=1)
    count: int = Field(default=5, ge=1, le=20)
    instructions: str = ""


class AcceptSuggestionCreate(BaseModel):
    suggestion_id: str
    actor: str = "human:web"


class SuggestFieldsCreate(BaseModel):
    entity_kind: str = Field(min_length=1)
    count: int = Field(default=5, ge=1, le=20)
    instructions: str = ""


class AcceptFieldSuggestionsCreate(BaseModel):
    suggestion_ids: list[str] = Field(min_length=1)
    actor: str = "human:web"


def create_app(
    database: str | Path | None = None,
    frontend: str | Path | None = None,
    research_runner: ResearchRunner | None = None,
    suggestion_runner: EntitySuggestionRunner | None = None,
    field_suggestion_runner: FieldSuggestionRunner | None = None,
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

    @app.get("/api/matrix/{entity_kind}")
    def matrix(
        entity_kind: str,
        questions: Annotated[list[str] | None, Query()] = None,
        known_at: str | None = None,
        valid_at: str | None = None,
    ) -> dict[str, Any]:
        return store().matrix(entity_kind, questions, known_at, valid_at)

    @app.get("/api/export/{entity_kind}.xlsx")
    def export_xlsx(entity_kind: str) -> FileResponse:
        matrix_data = store().matrix(entity_kind)
        with NamedTemporaryFile(prefix="epiq-", suffix=".xlsx", delete=False) as temporary:
            output = Path(temporary.name)
        write_xlsx(matrix_data, output)
        safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "-", entity_kind).strip("-") or "table"
        return FileResponse(
            output,
            filename=f"{safe_kind}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            background=BackgroundTask(output.unlink, missing_ok=True),
        )

    @app.post("/api/entities", status_code=201)
    def add_entity(body: EntityCreate) -> dict[str, str]:
        entity_id = store().add_entity(body.kind, body.name, body.attributes, body.actor)
        return {"entity_id": entity_id}

    @app.post("/api/entities/{entity_id}/aliases", status_code=201)
    def add_entity_alias(entity_id: str, body: EntityAliasCreate) -> dict[str, str]:
        return {"alias_id": store().add_entity_alias(entity_id, body.alias, body.actor)}

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
        )
        return {"source_id": source_id, "evidence_id": evidence_id}

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
            jobs[job_id]["status"] = "running"
            jobs[job_id]["started_at"] = datetime.now(UTC).isoformat()
            persist_job(job_id)

        def progress(message: str) -> None:
            with app.state.research_lock:
                jobs[job_id]["messages"].append(
                    {"at": datetime.now(UTC).isoformat(), "message": message}
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
                    targets.append({"entity_id": row["entity_id"], "name": row["name"]})
                elif body.mode == "retry_not_found" and cell["state"] == "NotFound":
                    targets.append({"entity_id": row["entity_id"], "name": row["name"]})
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
                            "existing_value": cell.get("value", cell.get("values")),
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
            }
            progress(
                f"Prepared {len(targets)} {body.entity_kind} row{'s' if len(targets) != 1 else ''}"
            )
            progress("Waiting for an available research slot")
            with app.state.research_semaphore:
                findings = app.state.research_runner(
                    body.entity_kind, research_question, targets, progress
                )
            target_ids = {item["entity_id"] for item in targets}
            targets_by_id = {item["entity_id"]: item for item in targets}
            for finding in findings:
                entity_id = str(finding["entity_id"])
                if entity_id not in target_ids:
                    continue
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
                elif finding["status"] == "answered":
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
                    )
                    evidence: str | list[str] = evidence_id
                    value = finding["value"]
                    if body.mode == "add_evidence":
                        target = targets_by_id[entity_id]
                        evidence = [*target["existing_evidence_ids"], evidence_id]
                        value = target["existing_value"]
                    valid_from = str(
                        (
                            target.get("existing_valid_from")
                            if body.mode == "add_evidence"
                            else finding.get("observed_as_of") or finding.get("source_published_at")
                        )
                        or datetime.now(UTC).date().isoformat()
                    )
                    project.assert_claim(
                        entity_id,
                        question["question_id"],
                        value,
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
                    jobs[job_id]["completed"] += 1
                    persist_job(job_id)
                progress(f"Finished research for {targets_by_id[entity_id]['name']}")
            with app.state.research_lock:
                jobs[job_id]["status"] = "completed"
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
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        job = {
            "job_id": job_id,
            "entity_kind": body.entity_kind,
            "question_id": body.question,
            "mode": body.mode,
            "instructions": body.instructions,
            "requested_entity_ids": body.entity_ids,
            "scope": body.scope,
            "status": "queued",
            "total": 0,
            "completed": 0,
            "target_entity_ids": [],
            "created_at": datetime.now(UTC).isoformat(),
            "error": None,
            "messages": [{"at": datetime.now(UTC).isoformat(), "message": "Research job queued"}],
        }
        with app.state.research_lock:
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

    @app.get("/api/research/jobs")
    def research_jobs() -> list[dict[str, Any]]:
        with app.state.research_lock:
            return list(reversed(list(app.state.research_jobs.values())))

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
