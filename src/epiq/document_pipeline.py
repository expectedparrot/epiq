"""Content-addressed documents and deterministic extraction pipelines."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .errors import EpiqError
from .ledger_api import LedgerDatabase


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _report(database: LedgerDatabase, report_id: str, report_type: str) -> dict[str, Any]:
    match = next((item for item in database.reports() if item["report_id"] == report_id), None)
    if match is None or match["report_type"] != report_type:
        raise EpiqError("missing_domain_object", f"Missing {report_type}: {report_id}")
    return match


def add_document(
    database: LedgerDatabase,
    input_path: str | Path,
    *,
    title: str | None = None,
    document_type: str = "source-document",
    mime_type: str | None = None,
    source_url: str | None = None,
    retention: str = "git",
    actor: str = "human:cli",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    path = Path(input_path)
    return add_document_bytes(
        database,
        path.read_bytes(),
        filename=path.name,
        title=title,
        document_type=document_type,
        mime_type=mime_type,
        source_url=source_url,
        retention=retention,
        actor=actor,
        idempotency_key=idempotency_key,
    )


def add_document_bytes(
    database: LedgerDatabase,
    content: bytes,
    *,
    filename: str,
    title: str | None = None,
    document_type: str = "source-document",
    mime_type: str | None = None,
    source_url: str | None = None,
    retention: str = "git",
    actor: str = "human:web",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if retention == "external":
        raise EpiqError(
            "invalid_artifact_retention",
            "Documents used by extraction pipelines must use git or git-lfs retention",
        )
    mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    result = database.record_report(
        "source-document",
        content,
        parameters={
            "title": title or filename,
            "filename": filename,
            "document_type": document_type,
            "mime_type": mime,
            "source_url": source_url,
            "source_digest": digest,
        },
        retention=retention,
        actor=actor,
        idempotency_key=idempotency_key or f"document:{digest}",
    )
    return {"document_id": result["report_id"], **result}


def define_pipeline(
    database: LedgerDatabase,
    input_path: str | Path,
    *,
    retention: str = "git",
    actor: str = "human:cli",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    content = Path(input_path).read_bytes()
    try:
        spec = json.loads(content)
    except json.JSONDecodeError as error:
        raise EpiqError("invalid_pipeline", "Pipeline definition must be valid JSON") from error
    return define_pipeline_spec(
        database,
        spec,
        retention=retention,
        actor=actor,
        idempotency_key=idempotency_key,
    )


def define_pipeline_spec(
    database: LedgerDatabase,
    spec: dict[str, Any],
    *,
    retention: str = "git",
    actor: str = "human:web",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise EpiqError("invalid_pipeline", "Pipeline definition must be a JSON object")
    name = spec.get("name")
    version = spec.get("version")
    outputs = spec.get("outputs")
    if not isinstance(name, str) or not name.strip() or not isinstance(version, int):
        raise EpiqError("invalid_pipeline", "Pipeline requires a name and integer version")
    if not isinstance(outputs, list) or not outputs:
        raise EpiqError("invalid_pipeline", "Pipeline requires at least one output")
    for output in outputs:
        if not isinstance(output, dict) or not all(
            isinstance(output.get(key), str) and output[key].strip()
            for key in ("kind", "question", "instructions")
        ):
            raise EpiqError(
                "invalid_pipeline",
                "Each output requires kind, question, and instructions",
            )
    digest = "sha256:" + hashlib.sha256(_canonical(spec)).hexdigest()
    result = database.record_report(
        "extraction-pipeline",
        _canonical(spec),
        parameters={"name": name, "version": version, "spec_digest": digest},
        retention=retention,
        actor=actor,
        idempotency_key=idempotency_key or f"pipeline:{name}:{version}:{digest}",
    )
    return {"pipeline_id": result["report_id"], "spec": spec, **result}


def _extract_text(content: bytes, mime_type: str) -> str:
    if mime_type == "application/pdf":
        try:
            completed = subprocess.run(
                ["pdftotext", "-layout", "-", "-"],
                input=content,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise EpiqError(
                "document_converter_unavailable",
                "PDF extraction requires the `pdftotext` executable",
            ) from error
        if completed.returncode:
            raise EpiqError("document_conversion_failed", completed.stderr.decode(errors="replace"))
        return completed.stdout.decode("utf-8", errors="replace")
    if mime_type.startswith("text/") or mime_type in {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
    }:
        return content.decode("utf-8", errors="replace")
    raise EpiqError(
        "unsupported_document_type",
        f"No text converter is configured for {mime_type}",
    )


def _chunks(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    if size < 1000 or size > 50000 or overlap < 0 or overlap >= size:
        raise EpiqError("invalid_pipeline", "chunk_chars must be 1000..50000 and overlap smaller")
    chunks = []
    start = 0
    while start < len(text):
        target = min(len(text), start + size)
        end = target
        if target < len(text):
            boundary = text.rfind("\n\n", start + size // 2, target)
            if boundary > start:
                end = boundary + 2
        chunks.append((start, end, text[start:end]))
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def prepare_pipeline(
    database: LedgerDatabase,
    document_id: str,
    pipeline_id: str,
    *,
    retention: str = "git",
    actor: str = "agent:cli",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    document = _report(database, document_id, "source-document")
    pipeline = _report(database, pipeline_id, "extraction-pipeline")
    store = ArtifactStore(database.path)
    spec = json.loads(store.read(pipeline["content_digest"]))
    raw = store.read(document["content_digest"])
    text = _extract_text(raw, str(document["parameters"]["mime_type"]))
    text_ref = store.retain(text.encode(), retention=retention)
    chunking = spec.get("chunking", {})
    size = int(chunking.get("chunk_chars", 12000))
    overlap = int(chunking.get("overlap_chars", 500))
    chunk_records = []
    for index, (start, end, chunk) in enumerate(_chunks(text, size, overlap)):
        reference = store.retain(chunk.encode(), retention=retention)
        chunk_records.append(
            {"index": index, "start": start, "end": end, "digest": reference.digest}
        )
    manifest = {
        "schema_version": 1,
        "document_id": document_id,
        "document_digest": document["content_digest"],
        "pipeline_id": pipeline_id,
        "pipeline_digest": pipeline["content_digest"],
        "extracted_text_digest": text_ref.digest,
        "chunks": chunk_records,
    }
    digest = hashlib.sha256(_canonical(manifest)).hexdigest()
    result = database.record_report(
        "extraction-run",
        _canonical(manifest),
        parameters={
            "document_id": document_id,
            "pipeline_id": pipeline_id,
            "status": "prepared",
            "chunk_count": len(chunk_records),
        },
        retention=retention,
        actor=actor,
        idempotency_key=idempotency_key or f"pipeline-run:{digest}",
    )
    return {"run_id": result["report_id"], "manifest": manifest, **result}


def read_pipeline_chunk(
    database: LedgerDatabase, run_id: str, index: int
) -> dict[str, Any]:
    run = _report(database, run_id, "extraction-run")
    store = ArtifactStore(database.path)
    manifest = json.loads(store.read(run["content_digest"]))
    chunks = manifest["chunks"]
    if index < 0 or index >= len(chunks):
        raise EpiqError("invalid_chunk", f"Chunk index must be between 0 and {len(chunks) - 1}")
    chunk = chunks[index]
    pipeline = _report(database, manifest["pipeline_id"], "extraction-pipeline")
    spec = json.loads(store.read(pipeline["content_digest"]))
    return {
        "run_id": run_id,
        "chunk": chunk,
        "content": store.read(chunk["digest"]).decode("utf-8"),
        "outputs": spec["outputs"],
    }


def apply_pipeline_findings(
    database: LedgerDatabase,
    run_id: str,
    findings: list[dict[str, Any]],
    *,
    actor: str = "agent:pipeline",
) -> dict[str, Any]:
    run = _report(database, run_id, "extraction-run")
    store = ArtifactStore(database.path)
    manifest = json.loads(store.read(run["content_digest"]))
    pipeline = _report(database, manifest["pipeline_id"], "extraction-pipeline")
    spec = json.loads(store.read(pipeline["content_digest"]))
    document = _report(database, manifest["document_id"], "source-document")
    allowed = {(item["kind"], item["question"]): item for item in spec["outputs"]}
    entities = {(item["kind"], item["name"]): item for item in database.entities()}
    questions = {(item["subject_kind"], item["name"]): item for item in database.questions()}
    chunks = {item["index"]: item for item in manifest["chunks"]}
    writes = []
    for offset, finding in enumerate(findings):
        required = {
            "chunk_index",
            "subject_kind",
            "subject_name",
            "question",
            "value",
            "quote",
            "valid_from",
        }
        if not isinstance(finding, dict) or not required <= set(finding):
            raise EpiqError(
                "invalid_pipeline_finding", f"Finding {offset} is missing required keys"
            )
        kind = str(finding["subject_kind"])
        question_name = str(finding["question"])
        output = allowed.get((kind, question_name))
        if output is None:
            raise EpiqError(
                "pipeline_output_not_allowed", f"Pipeline does not allow {kind}.{question_name}"
            )
        subject_name = str(finding["subject_name"])
        entity = entities.get((kind, subject_name))
        question = questions.get((kind, question_name))
        if entity is None and output.get("create_subjects") is True:
            created = database.create_entity(
                kind,
                subject_name,
                role=str(output.get("subject_role") or "entity"),
                actor=actor,
                idempotency_key=f"pipeline:{run_id}:subject:{kind}:{subject_name}",
            )
            entity = next(
                item for item in database.entities() if item["entity_id"] == created["entity_id"]
            )
            entities[(kind, subject_name)] = entity
        if entity is None or question is None:
            raise EpiqError(
                "missing_domain_object",
                f"Missing row or field: {kind}.{finding['subject_name']}.{question_name}",
            )
        chunk = chunks.get(int(finding["chunk_index"]))
        if chunk is None:
            raise EpiqError("invalid_chunk", f"Unknown chunk: {finding['chunk_index']}")
        chunk_text = store.read(chunk["digest"]).decode("utf-8")
        quote = str(finding["quote"])
        local_start = chunk_text.find(quote)
        if not quote or local_start < 0:
            raise EpiqError(
                "support_not_captured", f"Finding {offset} quote is absent from its chunk"
            )
        locator = {
            "document_id": manifest["document_id"],
            "document_digest": manifest["document_digest"],
            "extracted_text_digest": manifest["extracted_text_digest"],
            "chunk_index": chunk["index"],
            "char_start": chunk["start"] + local_start,
            "char_end": chunk["start"] + local_start + len(quote),
        }
        key_material = _canonical({"run_id": run_id, "finding": finding})
        key = hashlib.sha256(key_material).hexdigest()
        value = finding["value"]
        value_type = str(question["value_type"])
        if value_type.startswith("Ref[") and value_type.endswith("]"):
            target_kind = value_type[4:-1]
            if not isinstance(value, str):
                raise EpiqError(
                    "invalid_reference_value", f"{kind}.{question_name} requires a row name"
                )
            target = entities.get((target_kind, value))
            if target is None and output.get("create_refs") is True:
                created = database.create_entity(
                    target_kind,
                    value,
                    actor=actor,
                    idempotency_key=f"pipeline:{run_id}:ref:{target_kind}:{value}",
                )
                target = next(
                    item
                    for item in database.entities()
                    if item["entity_id"] == created["entity_id"]
                )
                entities[(target_kind, value)] = target
            if target is None:
                raise EpiqError(
                    "missing_domain_object", f"Missing referenced row: {target_kind}.{value}"
                )
            value = target["entity_id"]
        result = database.record(
            entity["entity_id"],
            question["question_id"],
            value,
            valid_from=str(finding["valid_from"]),
            source_title=str(document["parameters"]["title"]),
            source_type="report",
            url=f"artifact:{manifest['extracted_text_digest']}#char={locator['char_start']}-{locator['char_end']}",
            retrieved_at=str(finding.get("retrieved_at") or datetime.now(UTC).date()),
            excerpt=quote,
            locator=locator,
            confidence=str(finding.get("confidence") or "medium"),
            actor=actor,
            idempotency_key=f"pipeline:{key}",
        )
        writes.append(
            {
                "subject": finding["subject_name"],
                "question": question_name,
                "claim_id": result["claim_id"],
            }
        )
    return {"run_id": run_id, "written": len(writes), "writes": writes}


class PiChunkExtractor:
    """Extract JSON findings from one bounded chunk without persistent conversation context."""

    def __init__(self, *, model: str | None = None, executable: str | None = None) -> None:
        self.model = model or os.environ.get("EPIQ_PIPELINE_MODEL", "openai/gpt-6.2-sol")
        self.executable = executable or os.environ.get("EPIQ_PI_EXECUTABLE", "pi")

    def __call__(
        self, chunk: dict[str, Any], outputs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        prompt = f"""Extract structured facts from one bounded document chunk.

Chunk index: {chunk['index']}
Allowed outputs and instructions:
{json.dumps(outputs, ensure_ascii=False, sort_keys=True)}

Document chunk:
<document-chunk>
{chunk['content']}
</document-chunk>

Return one JSON object with a `findings` array. Each finding must contain chunk_index,
subject_kind, subject_name, question, value, quote, valid_from, and confidence. The quote must be a
short exact verbatim substring of this chunk. Use only allowed kind/question pairs. Emit no finding
when the chunk does not directly support one. Never infer a fact from filename, pipeline guidance,
or general knowledge. For relationship rows, use a stable descriptive subject_name consistently
across fields. Return JSON only."""
        command = [
            self.executable,
            "--print",
            "--mode",
            "json",
            "--no-builtin-tools",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--model",
            self.model,
            "--thinking",
            os.environ.get("EPIQ_PIPELINE_THINKING", "low"),
            prompt,
        ]
        completed = subprocess.run(
            command, text=True, capture_output=True, timeout=300, check=False
        )
        if completed.returncode:
            raise EpiqError(
                "pipeline_extractor_failed",
                (completed.stderr or completed.stdout or "Pi failed")[-1000:],
            )
        response = ""
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "message_end" and event.get("message", {}).get(
                "role"
            ) == "assistant":
                response = "".join(
                    str(item.get("text", ""))
                    for item in event["message"].get("content", [])
                    if item.get("type") == "text"
                )
        try:
            start, end = response.index("{"), response.rindex("}") + 1
            payload = json.loads(response[start:end])
            findings = payload["findings"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise EpiqError(
                "pipeline_extractor_invalid", "Extractor returned invalid JSON"
            ) from error
        if not isinstance(findings, list):
            raise EpiqError("pipeline_extractor_invalid", "findings must be an array")
        return findings


def extract_pipeline(
    database: LedgerDatabase,
    run_id: str,
    extractor: Any,
    *,
    workers: int = 4,
    actor: str = "agent:pipeline",
    progress: Any | None = None,
    skip_chunks: set[int] | None = None,
) -> dict[str, Any]:
    run = _report(database, run_id, "extraction-run")
    store = ArtifactStore(database.path)
    manifest = json.loads(store.read(run["content_digest"]))
    pipeline = _report(database, manifest["pipeline_id"], "extraction-pipeline")
    spec = json.loads(store.read(pipeline["content_digest"]))
    all_chunks = [
        {
            **item,
            "content": store.read(item["digest"]).decode("utf-8"),
        }
        for item in manifest["chunks"]
    ]
    skipped = set(skip_chunks or ())
    chunks = [item for item in all_chunks if int(item["index"]) not in skipped]
    worker_count = max(1, min(int(workers), 8))
    writes: list[dict[str, Any]] = []
    finding_count = 0
    failures = []
    apply_lock = threading.Lock()
    seen: set[bytes] = set()
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="epiq-pipeline") as pool:
        futures = {pool.submit(extractor, chunk, spec["outputs"]): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                extracted = future.result()
                if not isinstance(extracted, list):
                    raise EpiqError(
                        "pipeline_extractor_invalid", "Extractor findings must be an array"
                    )
                accepted = []
                with apply_lock:
                    for finding in extracted:
                        if not isinstance(finding, dict):
                            continue
                        semantic = _canonical(
                            {
                                key: finding.get(key)
                                for key in (
                                    "subject_kind",
                                    "subject_name",
                                    "question",
                                    "value",
                                )
                            }
                        )
                        if semantic in seen:
                            continue
                        seen.add(semantic)
                        accepted.append(finding)
                    if accepted:
                        applied = apply_pipeline_findings(
                            database, run_id, accepted, actor=actor
                        )
                        writes.extend(applied["writes"])
                        finding_count += len(accepted)
                if progress:
                    progress(chunk["index"], "completed", len(accepted))
            except Exception as error:  # keep other chunks useful
                failures.append({"chunk_index": chunk["index"], "error": str(error)})
                if progress:
                    progress(chunk["index"], "failed", 0)
    return {
        "run_id": run_id,
        "written": len(writes),
        "writes": writes,
        "chunks": len(all_chunks),
        "attempted_chunks": len(chunks),
        "skipped_completed_chunks": len(skipped),
        "completed_chunks": len(chunks) - len(failures),
        "failed_chunks": failures,
        "extracted_findings": finding_count,
    }

