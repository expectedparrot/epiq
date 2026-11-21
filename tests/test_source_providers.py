import json
import threading
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from epiq.provenance import digest
from epiq.source_contracts import (
    SourceAttempt,
    SourceBundle,
    SourceDocument,
    SourceError,
    SourceRequest,
)
from epiq.source_http import HTTPResult, fetch_public, public_url
from epiq.source_providers import (
    FirecrawlSourceProvider,
    FixtureSourceProvider,
    URLSourceProvider,
    normalize_bundle,
)
from epiq.source_research import SourceResearchRunner, validate_finding


def request(**kwargs):
    return SourceRequest(
        request_id="req",
        research_task_id="job",
        query="A population",
        entity_context={"entity_id": "a", "name": "A", "aliases": ["A town"]},
        question_context={"name": "population", "value_type": "Int"},
        **kwargs,
    )


def document(**kwargs):
    return SourceDocument(
        source_ref="s",
        canonical_url="https://example.test/a",
        title="A census",
        excerpt="Population 7.",
        markdown="Population 7. More text.",
        retrieved_at=datetime.now(UTC),
        provider="fixture",
        **kwargs,
    )


def bundle(req=None, docs=None):
    req = req or request()
    return SourceBundle(
        request_id=req.request_id, provider="fixture", documents=docs or [document()], attempts=[]
    )


def response(payload, status=200, headers=None, url="https://api.firecrawl.dev/v2/scrape"):
    return HTTPResult(status, url, headers or {}, json.dumps(payload).encode())


def test_contract_round_trip_limits_and_hashes():
    req = request()
    assert SourceRequest.model_validate_json(req.model_dump_json()) == req
    source = bundle()
    assert SourceBundle.model_validate_json(source.model_dump_json()) == source
    with pytest.raises(ValidationError):
        req.query = "changed"
    with pytest.raises(ValidationError):
        request(max_sources=21)
    with pytest.raises(ValidationError):
        document(content_hash="wrong")
    with pytest.raises(ValidationError):
        SourceDocument(**(document().model_dump() | {"retrieved_at": datetime(2026, 1, 1)}))
    with pytest.raises(ValidationError):
        SourceBundle(
            request_id="req", provider="fixture", documents=[document(), document()], attempts=[]
        )


def test_normalization_excludes_urls_and_duplicate_content():
    first = document()
    docs = [
        first,
        first.model_copy(
            update={"source_ref": "duplicate", "canonical_url": "https://mirror.test/a"}
        ),
        first.model_copy(
            update={
                "source_ref": "known",
                "canonical_url": "https://known.test/?utm_source=x",
                "markdown": "Different text.",
                "excerpt": "Different",
            }
        ),
    ]
    normalized = normalize_bundle(bundle(docs=docs), request(exclude_urls=["https://known.test/"]))
    assert len(normalized.documents) == 1
    assert normalized.documents[0].content_hash == digest(first.markdown)
    assert (
        normalize_bundle(bundle(), request(exclude_hashes=[digest(first.markdown)])).documents == []
    )


def test_capability_failure_precedes_network():
    provider = URLSourceProvider(transport=lambda *args, **kwargs: pytest.fail("network called"))
    with pytest.raises(SourceError) as error:
        provider.collect(request())
    assert error.value.code == "unsupported_capability"
    with pytest.raises(SourceError):
        provider.collect(request(urls=["https://example.test"], published_after="2026-01-01"))


def test_firecrawl_search_then_capture_preserves_context_and_partial_failure():
    calls = []

    def transport(url, **kwargs):
        payload = json.loads(kwargs["body"])
        calls.append((url, payload))
        if url.endswith("/search"):
            return response(
                {
                    "success": True,
                    "creditsUsed": 2,
                    "data": {
                        "web": [
                            {"url": "https://example.test/a", "title": "A"},
                            {"url": "https://example.test/b", "title": "B"},
                        ]
                    },
                }
            )
        if payload["url"].endswith("/b"):
            return response({}, status=429, headers={"retry-after": "12"})
        return response(
            {
                "success": True,
                "data": {
                    "markdown": "Population 7.",
                    "metadata": {"title": "Census", "sourceURL": payload["url"]},
                },
            }
        )

    provider = FirecrawlSourceProvider(api_key="unit-test-secret", transport=transport)
    captured = provider.collect(request())
    assert len(captured.documents) == 1
    assert captured.documents[0].markdown == "Population 7."
    assert captured.attempts[-1].error_code == "rate_limited"
    assert captured.attempts[-1].retry_after == 12
    assert calls[0][0].endswith("/search")
    assert calls[1][1]["formats"] == ["markdown"]
    assert "unit-test-secret" not in captured.model_dump_json()


@pytest.mark.parametrize("payload", [{}, {"data": {"web": "bad"}}, {"success": False}])
def test_firecrawl_malformed_response_has_stable_error(payload):
    provider = FirecrawlSourceProvider(
        api_key="x", transport=lambda *args, **kwargs: response(payload)
    )
    captured = provider.collect(request())
    assert captured.documents == []
    assert captured.attempts[0].error_code == "invalid_response"


def test_firecrawl_discovery_is_not_captured_evidence():
    provider = FirecrawlSourceProvider(
        api_key="x",
        transport=lambda *args, **kwargs: response(
            {
                "data": {
                    "web": [
                        {"url": "https://example.test", "title": "Result", "description": "Snippet"}
                    ]
                }
            }
        ),
    )
    collected = provider.collect(request(capture="metadata"))
    doc = collected.documents[0]
    assert doc.capture_method == "discovery"
    assert doc.markdown is None and doc.content_hash is None
    with pytest.raises(SourceError) as error:
        validate_finding(
            {
                "entity_id": "a",
                "status": "answered",
                "value_json": "7",
                "source_refs": [doc.source_ref],
                "support_quotes": {doc.source_ref: "Snippet"},
            },
            request(),
            collected,
        )
    assert error.value.code == "uncaptured_citation"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://[::1]",
        "http://169.254.169.254",
        "http://localhost",
        "http://example.local",
        "file:///tmp/x",
        "https://user:password@example.test",
    ],
)
def test_direct_fetch_blocks_private_or_credentialed_urls(url):
    with pytest.raises(SourceError):
        public_url(url)


def test_dns_private_address_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 80))]
    )
    monkeypatch.setattr(
        "socket.create_connection", lambda *args, **kwargs: pytest.fail("private connection")
    )
    with pytest.raises(SourceError):
        fetch_public("https://public-looking.test/")


def test_url_capture_and_redirect_filtering():
    def transport(url):
        return HTTPResult(
            200,
            url,
            {"content-type": "text/html"},
            b"<h1>A</h1><script>secret()</script><p>Population 7.</p>",
        )

    collected = URLSourceProvider(transport=transport).collect(
        request(urls=["https://example.test/a"])
    )
    assert "Population 7." in collected.documents[0].markdown
    assert "secret" not in collected.documents[0].markdown
    excluded = URLSourceProvider(
        transport=lambda url: HTTPResult(200, "http://127.0.0.1", {}, b"x")
    )
    collected = excluded.collect(request(urls=["https://example.test/a"]))
    assert not collected.documents
    assert collected.attempts[0].error_code == "request_failed"


@pytest.mark.parametrize(
    ("refs", "quotes", "code"),
    [
        (["invented"], {"invented": "Population 7."}, "uncaptured_citation"),
        (["s"], {"s": "Unsupported quote"}, "support_not_captured"),
    ],
)
def test_reasoning_cannot_invent_sources_or_quotes(refs, quotes, code):
    with pytest.raises(SourceError) as error:
        validate_finding(
            {
                "entity_id": "a",
                "status": "answered",
                "value_json": "7",
                "source_refs": refs,
                "support_quotes": quotes,
            },
            request(),
            bundle(),
        )
    assert error.value.code == code


def test_incremental_runner_yields_ready_entity_and_passes_context_unchanged():
    waiting = threading.Event()
    released = threading.Event()
    seen = []

    def collect(req):
        seen.append(req)
        if req.entity_context["entity_id"] == "slow":
            waiting.set()
            assert released.wait(5)
        return bundle(req)

    provider = FixtureSourceProvider(collect)

    def reasoner(req, sources):
        return {
            "entity_id": req.entity_context["entity_id"],
            "status": "answered",
            "value_json": "7",
            "source_refs": ["s"],
            "support_quotes": {"s": "Population 7."},
        }

    runner = SourceResearchRunner(provider, reasoner, concurrency=2)
    context = {"project_name": "Ambiguous towns", "peer_rows": ["A elsewhere"]}
    question = {
        "name": "population",
        "value_type": "Int",
        "definition": {},
        "research_context": context,
    }
    entities = [
        {"entity_id": "slow", "name": "A"},
        {"entity_id": "fast", "name": "A", "aliases": ["Town A"]},
    ]
    findings = runner.iter_findings("Town", question, entities)
    try:
        assert next(findings)["entity_id"] == "fast"
        assert waiting.is_set()
        assert all(req.question_context == question for req in seen)
        assert seen[1].entity_context["aliases"] == ["Town A"]
        assert seen[1].entity_context["research_context"] == context
        released.set()
        assert next(findings)["entity_id"] == "slow"
    finally:
        released.set()
        findings.close()


def test_failed_retrieval_does_not_become_not_found():
    def collect(req):
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
                    detail="Provider rate limit reached",
                    retryable=True,
                ),
            ],
        )

    audits = []
    runner = SourceResearchRunner(
        FixtureSourceProvider(collect), lambda *args: pytest.fail("reasoner called")
    )
    with pytest.raises(SourceError) as error:
        list(
            runner.iter_findings(
                "X",
                {"name": "q", "definition": {}},
                [{"entity_id": "a", "name": "A"}],
                on_bundle=lambda req, bundle: audits.append(bundle),
            )
        )
    assert error.value.code == "rate_limited"
    assert len(audits) == 1


def test_url_provider_preserves_success_before_invalid_url_and_rate_limit():
    def transport(url):
        if url.endswith("limited"):
            return HTTPResult(429, url, {"retry-after": "12"}, b"")
        return HTTPResult(200, url, {"content-type": "text/plain"}, b"Population 7.")

    result = URLSourceProvider(transport).collect(
        request(urls=["https://example.test/a", "http://[broken", "https://example.test/limited"])
    )
    assert len(result.documents) == 1
    assert [a.status for a in result.attempts] == ["completed", "failed", "failed"]
    assert result.attempts[-1].error_code == "rate_limited"
    assert result.attempts[-1].retry_after == 12


def test_composite_metadata_stops_after_discovery():
    from epiq.source_providers import CompositeSourceProvider

    def discover(req):
        return SourceBundle(
            request_id=req.request_id,
            provider="fixture",
            documents=[SourceDocument(
                source_ref="hit", canonical_url="https://example.test/a", title="Census",
                excerpt="Population", retrieved_at=datetime.now(UTC), provider="fixture",
                capture_method="discovery",
            )],
            attempts=[],
        )

    def unexpected_fetch(url):
        raise AssertionError("Metadata requests must not fetch")

    provider = CompositeSourceProvider(
        FixtureSourceProvider(discover), URLSourceProvider(unexpected_fetch)
    )
    assert provider.collect(request(capture="metadata")).documents[0].capture_method == "discovery"


def test_checked_json_rejects_nonfinite_response():
    from epiq.source_http import checked_json

    with pytest.raises(SourceError, match="invalid JSON"):
        checked_json(HTTPResult(200, "https://example.test", {}, b'{"creditsUsed": NaN}'))


def test_cancellation_prevents_any_provider_request():
    def transport(url, **kwargs):
        raise AssertionError("Cancelled task must not call provider")

    with pytest.raises(SourceError) as error:
        FirecrawlSourceProvider(api_key="fixture", transport=transport).collect(
            request(), cancelled=lambda: True
        )
    assert error.value.code == "cancelled"
