import os
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from epiq.source_contracts import SourceAttempt, SourceBundle, SourceDocument
from epiq.source_providers import FixtureSourceProvider
from epiq.store import Store
from epiq.web import create_app


def setup(tmp_path, provider, reasoner):
    store = Store(tmp_path / "source-web.sqlite")
    store.initialize("Towns")
    entity = store.add_entity("Town", "A", {}, "setup")
    question = store.add_question("population", "Town", "Int", {}, "setup")
    client = TestClient(create_app(store.path, source_provider=provider, source_reasoner=reasoner))
    return store, client, entity, question


def document(req):
    return SourceBundle(
        request_id=req.request_id,
        provider="fixture",
        documents=[
            SourceDocument(
                source_ref="source",
                canonical_url="https://example.test/a",
                title="Census",
                excerpt="Population 7.",
                markdown="Census for A. Population 7.",
                retrieved_at=datetime.now(UTC),
                provider="fixture",
            )
        ],
        attempts=[],
    )


def reasoner(req, bundle):
    return {
        "entity_id": req.entity_context["entity_id"],
        "status": "answered",
        "value_json": "7",
        "source_refs": ["source"],
        "support_quotes": {"source": "Population 7."},
    }


def wait_for_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/research/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Job did not finish")


def test_source_pipeline_persists_audit_support_and_review(tmp_path):
    store, client, entity, question = setup(tmp_path, FixtureSourceProvider(document), reasoner)
    with client:
        job = client.post(
            "/api/research/jobs", json={"entity_kind": "Town", "question": question}
        ).json()
        result = wait_for_job(client, job["job_id"])
        assert result["status"] == "completed", result
        assert result["written"] == 1
        sources = client.get(f"/api/research/jobs/{job['job_id']}/sources").json()
        assert len(sources) == 1
        assert "markdown" not in sources[0]["bundle"]["documents"][0]
        assert sources[0]["request"]["entity_context"]["entity_id"] == entity
        cell = store.matrix("Town")["rows"][0]["cells"]["population"]
        assert cell["value"] == 7
        assert cell["assurance"] == {"captured": True, "supported": True, "reviewed": False}
        claim = cell["lineage"][0]["claim_id"]
        reviewed = client.post(
            f"/api/claims/{claim}/review", json={"reason": "Checked census"}
        ).json()
        assert reviewed["status"] == "accepted"
        assert store.matrix("Town")["rows"][0]["cells"]["population"]["assurance"]["reviewed"]


def test_source_pipeline_records_retrieval_failure_without_not_found(tmp_path):
    def failed(req):
        at = datetime.now(UTC)
        return SourceBundle(
            request_id=req.request_id,
            provider="fixture",
            documents=[],
            attempts=[
                SourceAttempt(
                    query=req.query,
                    status="failed",
                    started_at=at,
                    finished_at=at,
                    error_code="rate_limited",
                    detail="Rate limited",
                    retry_after=15,
                    retryable=True,
                ),
            ],
        )

    store, client, entity, question = setup(tmp_path, FixtureSourceProvider(failed), reasoner)
    with client:
        job = client.post(
            "/api/research/jobs", json={"entity_kind": "Town", "question": question}
        ).json()
        result = wait_for_job(client, job["job_id"])
        assert result["status"] == "failed"
        assert result["error_detail"]["code"] == "rate_limited"
        assert result["error_detail"]["retry_after"] == 15
        assert len(store.source_bundles(job["job_id"])) == 1
        assert store.matrix("Town")["rows"][0]["cells"]["population"]["state"] == "Unasked"


def test_cancelled_source_work_cannot_write_late_results(tmp_path):
    started, release = threading.Event(), threading.Event()

    def blocking(req):
        started.set()
        assert release.wait(5)
        return document(req)

    store, client, entity, question = setup(tmp_path, FixtureSourceProvider(blocking), reasoner)
    with client:
        job = client.post(
            "/api/research/jobs", json={"entity_kind": "Town", "question": question}
        ).json()
        try:
            assert started.wait(2)
            cancelled = client.post(f"/api/research/jobs/{job['job_id']}/cancel")
            assert cancelled.status_code == 200
            result = wait_for_job(client, job["job_id"])
            assert result["status"] == "cancelled"
        finally:
            release.set()
        assert store.list_claims() == []
        assert store.source_bundles() == []


def test_added_evidence_excludes_existing_url_before_reasoning(tmp_path):
    seen = []

    def provider(req):
        seen.append(req)
        return document(req)

    store, client, entity, question = setup(tmp_path, FixtureSourceProvider(provider), reasoner)
    with client:
        first = client.post(
            "/api/research/jobs", json={"entity_kind": "Town", "question": question}
        ).json()
        assert wait_for_job(client, first["job_id"])["status"] == "completed"
        second = client.post(
            "/api/research/jobs",
            json={"entity_kind": "Town", "question": question, "mode": "add_evidence"},
        ).json()
        assert wait_for_job(client, second["job_id"])["status"] == "completed"
        assert seen[-1].exclude_urls == ["https://example.test/a"]
        assert len(store.matrix("Town")["rows"][0]["cells"]["population"]["lineage"]) == 1


@pytest.mark.skipif(
    os.environ.get("EPIQ_LIVE_SOURCE_SMOKE") != "1",
    reason="Set EPIQ_LIVE_SOURCE_SMOKE=1 to authorize live Firecrawl and EDSL calls",
)
def test_live_firecrawl_followed_by_edsl_remote_inference():
    from epiq.source_providers import FirecrawlSourceProvider
    from epiq.source_research import EDSLSourceReasoner, SourceResearchRunner

    assert os.environ.get("FIRECRAWL_API_KEY") and os.environ.get("EXPECTED_PARROT_API_KEY")
    runner = SourceResearchRunner(FirecrawlSourceProvider(), EDSLSourceReasoner(remote=True))
    findings = list(
        runner.iter_findings(
            "Website",
            {
                "name": "page_heading",
                "value_type": "String",
                "definition": {
                    "source_request": {"urls": ["https://example.com/"], "max_sources": 1}
                },
                "instructions": "Return the example.com page heading exactly as written.",
            },
            [{"entity_id": "example", "name": "Example Domain"}],
        )
    )
    assert findings[0]["status"] == "answered"
    assert findings[0]["value"] == "Example Domain"
    assert findings[0]["_captured_sources"][0]["provider"] == "firecrawl"


def test_relationship_approval_preserves_captured_evidence_and_support(tmp_path):
    def related(req, bundle):
        return {**reasoner(req, bundle), "value_json": '"Ada"'}

    store, client, entity, _ = setup(tmp_path, FixtureSourceProvider(document), related)
    store.add_entity("Person", "Ada", {}, "setup")
    question = store.add_question("mayor", "Town", "Ref[Person]", {}, "setup")
    with client:
        job = client.post(
            "/api/research/jobs", json={"entity_kind": "Town", "question": question}
        ).json()
        result = wait_for_job(client, job["job_id"])
        assert result["status"] == "completed", result
        assert result["outcome"] == "proposals"
        assert store.list_claims() == []
        suggestions = result["relationship_suggestions"]
        accepted = client.post(
            f"/api/research/jobs/{job['job_id']}/relationships/accept",
            json={"suggestion_ids": [item["suggestion_id"] for item in suggestions]},
        )
        assert accepted.status_code == 201, accepted.text
        cell = store.matrix("Town")["rows"][0]["cells"]["mayor"]
        assert cell["assurance"] == {"captured": True, "supported": True, "reviewed": True}
        assert len(cell["lineage"]) == 1
        assert cell["lineage"][0]["provenance"]["provider"] == "fixture"
