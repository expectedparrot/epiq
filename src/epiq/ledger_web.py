"""FastAPI surface backed exclusively by the authoritative Git ledger."""

from __future__ import annotations

import os
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


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    actor: str = "human:web"


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


def create_ledger_app(
    project: str | Path | None = None, frontend: str | Path | None = None
) -> FastAPI:
    """Create the ledger-only local web API."""
    root = Path(project or os.environ.get("EPIQ_PROJECT", ".epiq/project")).resolve()
    checkout_frontend = Path(__file__).parents[2] / "web" / "dist"
    packaged_frontend = Path(__file__).parent / "_frontend"
    frontend_path = Path(
        frontend or (packaged_frontend if packaged_frontend.exists() else checkout_frontend)
    ).resolve()
    app = FastAPI(title="Epiq", version="0.1.0")
    app.state.project = root
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
        return LedgerDatabase(root)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "project": "ready" if root.exists() else "uninitialized",
            "path": str(root),
            "initialized": root.exists(),
            "storage": "git-ledger",
        }

    @app.post("/api/project", status_code=201)
    def create_project(body: ProjectCreate) -> dict[str, Any]:
        created = LedgerDatabase(root, create=True, name=body.name, actor=body.actor)
        return {"project": created.project(), "path": str(root)}

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

    @app.get("/api/claims-long")
    def claims(kind: str | None = None, question: str | None = None) -> dict[str, Any]:
        items = database().claims(kind=kind, question=question)
        return {"count": len(items), "claims": items}

    @app.get("/api/matrix/{entity_kind}")
    def matrix(entity_kind: str) -> dict[str, Any]:
        return _web_matrix(database().matrix(entity_kind))

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
    def question_challenges() -> list[dict[str, Any]]:
        return []

    @app.get("/api/research/jobs")
    def research_jobs() -> list[dict[str, Any]]:
        return []

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
            return FileResponse(candidate)

    return app


def _web_matrix(projection: dict[str, Any]) -> dict[str, Any]:
    questions = projection["questions"]
    rows = []
    for entity in projection["entities"]:
        cells: dict[str, Any] = {}
        for question in questions:
            claims = projection["cells"][entity["entity_id"]][question["question_id"]]
            values = [claim["value"] for claim in claims]
            state = "Unasked" if not claims else "Answered" if len(claims) == 1 else "Contested"
            cells[question["name"]] = {
                "state": state,
                "value": values[0] if len(values) == 1 else None,
                "values": values,
                "claims": claims,
                "lineage": [],
            }
        rows.append({**entity, "cells": cells})
    return {"entity_kind": projection["kind"], "questions": questions, "rows": rows}
