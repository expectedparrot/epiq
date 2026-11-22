import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from epiq.document_pipeline import (
    add_document,
    apply_pipeline_findings,
    define_pipeline,
    extract_pipeline,
    prepare_pipeline,
    read_pipeline_chunk,
)
from epiq.errors import EpiqError
from epiq.ledger import LedgerRepository
from epiq.ledger_api import LedgerDatabase
from epiq.ledger_web import create_ledger_app


def database(tmp_path: Path) -> LedgerDatabase:
    root = tmp_path / "project"
    LedgerRepository.initialize(
        root,
        "Documents",
        actor="human:test",
        recorded_at="2026-11-22T12:00:00Z",
        project_id="prj_documents",
        replica_id="rep_documents",
    )
    db = LedgerDatabase(root)
    db.define_kind("EarningsCall", actor="human:test")
    db.create_entity("EarningsCall", "Upwork Q3 2026", actor="human:test")
    db.define_question("call_date", "EarningsCall", "Date", actor="human:test")
    return db


def test_document_pipeline_prepares_bounded_chunks_and_applies_quoted_finding(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    source = tmp_path / "transcript.txt"
    quote = "The call took place on November 7, 2026."
    source.write_text(quote + "\n\n" + ("Prepared remarks. " * 100))
    definition = tmp_path / "pipeline.json"
    definition.write_text(
        json.dumps(
            {
                "name": "earnings-call-facts",
                "version": 1,
                "chunking": {"chunk_chars": 1000, "overlap_chars": 100},
                "outputs": [
                    {
                        "kind": "EarningsCall",
                        "question": "call_date",
                        "instructions": "Extract an explicitly stated call date.",
                    }
                ],
            }
        )
    )

    document = add_document(db, source, title="Upwork Q3 2026 transcript")
    pipeline = define_pipeline(db, definition)
    run = prepare_pipeline(db, document["document_id"], pipeline["pipeline_id"])
    chunk = read_pipeline_chunk(db, run["run_id"], 0)

    assert quote in chunk["content"]
    assert len(chunk["content"]) <= 1000
    result = apply_pipeline_findings(
        db,
        run["run_id"],
        [
            {
                "chunk_index": 0,
                "subject_kind": "EarningsCall",
                "subject_name": "Upwork Q3 2026",
                "question": "call_date",
                "value": "2026-11-07",
                "quote": quote,
                "valid_from": "2026-11-07",
                "retrieved_at": "2026-11-22",
                "confidence": "high",
            }
        ],
    )

    assert result["written"] == 1
    matrix = db.matrix("EarningsCall")
    claim = next(iter(matrix["cells"].values()))[
        matrix["questions"][0]["question_id"]
    ][0]
    assert claim["value"] == "2026-11-07"
    evidence = db.view.evidence(evidence_ids=claim["evidence_ids"])[0]
    assert evidence["locator"]["chunk_index"] == 0


def test_document_pipeline_rejects_quote_absent_from_chunk(tmp_path: Path) -> None:
    db = database(tmp_path)
    source = tmp_path / "transcript.txt"
    source.write_text("Known text. " * 100)
    definition = tmp_path / "pipeline.json"
    definition.write_text(
        json.dumps(
            {
                "name": "facts",
                "version": 1,
                "outputs": [
                    {
                        "kind": "EarningsCall",
                        "question": "call_date",
                        "instructions": "Extract the date.",
                    }
                ],
            }
        )
    )
    document = add_document(db, source)
    pipeline = define_pipeline(db, definition)
    run = prepare_pipeline(db, document["document_id"], pipeline["pipeline_id"])

    with pytest.raises(EpiqError, match="absent from its chunk"):
        apply_pipeline_findings(
            db,
            run["run_id"],
            [
                {
                    "chunk_index": 0,
                    "subject_kind": "EarningsCall",
                    "subject_name": "Upwork Q3 2026",
                    "question": "call_date",
                    "value": "2026-11-07",
                    "quote": "Invented support.",
                    "valid_from": "2026-11-07",
                }
            ],
        )


def test_document_pipeline_extracts_chunks_and_deduplicates_same_fact(tmp_path: Path) -> None:
    db = database(tmp_path)
    source = tmp_path / "transcript.txt"
    source.write_text(("The call date was November 7, 2026.\n\n" + "Detail. " * 140) * 2)
    definition = tmp_path / "pipeline.json"
    definition.write_text(
        json.dumps(
            {
                "name": "facts",
                "version": 1,
                "chunking": {"chunk_chars": 1000, "overlap_chars": 100},
                "outputs": [
                    {
                        "kind": "EarningsCall",
                        "question": "call_date",
                        "instructions": "Extract the date.",
                    }
                ],
            }
        )
    )
    document = add_document(db, source)
    pipeline = define_pipeline(db, definition)
    run = prepare_pipeline(db, document["document_id"], pipeline["pipeline_id"])
    calls = []
    progress = []

    def extractor(chunk, _outputs):
        calls.append(chunk["index"])
        quote = "The call date was November 7, 2026."
        if quote not in chunk["content"]:
            return []
        return [
            {
                "chunk_index": chunk["index"],
                "subject_kind": "EarningsCall",
                "subject_name": "Upwork Q3 2026",
                "question": "call_date",
                "value": "2026-11-07",
                "quote": quote,
                "valid_from": "2026-11-07",
            }
        ]

    result = extract_pipeline(
        db,
        run["run_id"],
        extractor,
        workers=3,
        progress=lambda index, status, count: progress.append((index, status, count)),
    )

    assert len(calls) == result["chunks"]
    assert result["written"] == 1
    assert result["failed_chunks"] == []
    assert len(progress) == result["chunks"]


def test_document_pipeline_resume_skips_checkpointed_chunks(tmp_path: Path) -> None:
    db = database(tmp_path)
    source = tmp_path / "transcript.txt"
    source.write_text("One supported fact.\n\n" + "Detail. " * 400)
    definition = tmp_path / "pipeline.json"
    definition.write_text(
        json.dumps(
            {
                "name": "resumable-facts",
                "version": 1,
                "chunking": {"chunk_chars": 1000, "overlap_chars": 100},
                "outputs": [
                    {
                        "kind": "EarningsCall",
                        "question": "call_date",
                        "instructions": "Extract the date.",
                    }
                ],
            }
        )
    )
    document = add_document(db, source)
    pipeline = define_pipeline(db, definition)
    run = prepare_pipeline(db, document["document_id"], pipeline["pipeline_id"])
    attempted = []

    result = extract_pipeline(
        db,
        run["run_id"],
        lambda chunk, _outputs: attempted.append(chunk["index"]) or [],
        workers=2,
        skip_chunks={0},
    )

    assert 0 not in attempted
    assert result["attempted_chunks"] == result["chunks"] - 1
    assert result["skipped_completed_chunks"] == 1


def test_document_pipeline_web_upload_define_and_prepare(tmp_path: Path) -> None:
    root = tmp_path / "web-project"
    client = TestClient(create_ledger_app(root))
    assert client.post("/api/project", json={"name": "Web documents"}).status_code == 201

    uploaded = client.post(
        "/api/documents?filename=call.txt&title=Call",
        content=("The call date was November 7, 2026.\n\n" + "Detail. " * 150),
        headers={"content-type": "text/plain"},
    )
    assert uploaded.status_code == 201
    document_id = uploaded.json()["document_id"]

    defined = client.post(
        "/api/pipelines",
        json={
            "spec": {
                "name": "call-facts",
                "version": 1,
                "outputs": [
                    {
                        "kind": "EarningsCall",
                        "question": "call_date",
                        "instructions": "Extract the date.",
                    }
                ],
            }
        },
    )
    assert defined.status_code == 201
    pipeline_id = defined.json()["pipeline_id"]

    prepared = client.post(
        "/api/pipelines/prepare",
        json={"document_id": document_id, "pipeline_id": pipeline_id},
    )
    assert prepared.status_code == 201
    assert prepared.json()["manifest"]["chunks"]
    assert len(client.get("/api/documents").json()) == 1
    assert len(client.get("/api/pipelines").json()) == 2


def test_pipeline_job_history_survives_app_restart(tmp_path: Path) -> None:
    root = tmp_path / "persistent-project"
    LedgerRepository.initialize(
        root,
        "Persistent jobs",
        actor="human:test",
        recorded_at="2026-11-22T12:00:00Z",
        project_id="prj_persistent",
        replica_id="rep_persistent",
    )
    db = LedgerDatabase(root)
    jobs_path = root / ".epiq" / "pipeline-jobs.json"
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": [
                    {
                        "job_id": "pipe_finished",
                        "run_id": "run_example",
                        "status": "completed",
                        "completed_chunk_indices": [0, 1],
                        "chunks_completed": 2,
                        "findings_written": 3,
                        "attempts": 1,
                        "events": [],
                    }
                ],
            }
        )
    )

    client = TestClient(create_ledger_app(db.path))

    jobs = client.get("/api/pipeline-jobs").json()
    assert jobs[0]["job_id"] == "pipe_finished"
    assert jobs[0]["completed_chunk_indices"] == [0, 1]

