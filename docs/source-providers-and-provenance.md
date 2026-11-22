# Source providers and provenance

Issues #2 and #8 introduce an optional retrieval pipeline and explicit evidence assurance. Core
SQLite, CLI, and Python storage remain dependency-free. Schema 15 adds capture artifacts, source
bundle audits, supporting spans, and append-only reviews; existing projects migrate automatically.
Older evidence is labeled legacy/unverified rather than retrospectively claimed to have been fetched.

## Enable the optional backend

```bash
uv sync --extra web --extra sources
export EPIQ_SOURCE_PROVIDER=firecrawl
# Set FIRECRAWL_API_KEY and EXPECTED_PARROT_API_KEY privately in your environment.
uv run --extra web --extra sources epiq-web
```

This selects Firecrawl retrieval followed by EDSL remote inference for cell/column research. EDSL
uses `EPIQ_SOURCE_REASONING_MODEL` (default `gpt-4o-mini`). Row discovery, field suggestions, and
workspace planning keep their existing runners and credentials. Leaving `EPIQ_SOURCE_PROVIDER`
unset preserves the existing research backend.

Use `EPIQ_SOURCE_PROVIDER=url` for direct HTML or text capture. This provider requires explicit URLs
in the question's instructions or `definition.source_request.urls`; it cannot search. For example,
include this object in a question definition:

```json
{
  "source_request": {
    "urls": ["https://example.com/"],
    "max_sources": 1,
    "retain_content": false
  }
}
```

Other options are `include_domains`, `exclude_domains`, `published_after`, `published_before`,
and `capture` (`metadata`, `excerpt`, or `markdown`). Unsupported capabilities fail before network
work. Publication bounds are inclusive; documents with unknown publication dates are excluded when
bounds are requested. Metadata-only discoveries cannot support an answered finding.

## Replace retrieval or reasoning

`SourceProvider.collect(request, progress=None, cancelled=None)` returns a `SourceBundle` and
advertises `SourceCapabilities`. The version 1 Pydantic contracts reject unknown fields, non-finite
JSON, oversized input, invalid hashes, and naive capture timestamps. Inspect their packaged JSON
schemas using `epiq agent schema source-request`, `epiq agent schema source-bundle`, and
`epiq agent schema research-finding`. `epiq agent schema batch-write` describes the storage write contract.

Available adapters:

- `FirecrawlSourceProvider`: bounded search followed by individual Markdown captures, or capture
  of explicit URLs. Uses the official [v2 search API](https://docs.firecrawl.dev/api-reference/endpoint/search)
  and [v2 scrape API](https://docs.firecrawl.dev/api-reference/endpoint/scrape).
- `URLSourceProvider`: direct public HTML/plain-text/Markdown retrieval, with HTML script/style
  content removed. No search, PDF, or publication-date inference.
- `FixtureSourceProvider`: a request-to-bundle callable or mapping for deterministic offline work.
- `CompositeSourceProvider`: discovery from one provider and capture from another. Metadata-only
  requests stop after discovery.

Custom providers can be registered with `epiq.source_providers.registry.register(name, factory)`
before app creation, or injected directly:

```python
from epiq.source_providers import URLSourceProvider
from epiq.source_research import EDSLSourceReasoner
from epiq.web import create_app

app = create_app(
    "project.sqlite",
    source_provider=URLSourceProvider(),
    source_reasoner=EDSLSourceReasoner(remote=True),
)
```

A custom reasoner is a callable `(SourceRequest, SourceBundle) -> dict`. It must return a typed
`ResearchFinding` with the requested entity ID, JSON-encoded value, supplied `source_refs`, and an
exact supporting quote for each cited source. Epiq takes URLs, capture dates, hashes, and provider
metadata from the bundle, never from model output. It checks quote membership before writing claims.
Entity identity, aliases, attributes, question definitions, and project/table context reach the
reasoner. Source content is treated as untrusted prompt data. Matching text establishes provenance,
not semantic entailment, source credibility, or factual truth.

## Bounds, failures, and audit

Requests allow at most 20 sources, 200,000 characters per capture, and 4.5 MB of serialized contract
content. Reasoning prompts are capped at 500,000 characters. Direct HTTP reads have a 30-second
socket timeout and bounded response size. Every DNS result and redirect is checked for a public
address; connections are pinned to the checked address. Authenticated provider redirects are refused.
Firecrawl still performs remote retrieval under its own service policy; local checks cannot inspect
that service's DNS or internal redirects.

`EPIQ_SOURCE_CONCURRENCY` defaults to three entity tasks per research job, capped at six. The existing
job queue separately bounds concurrent jobs. Ready entity results commit incrementally. Cancellation
prevents subsequent audit/claim writes; it does not cancel an already running remote request or
refund its cost. Provider authentication, rate limits, invalid responses, and request failures are
structured errors. Completed captures survive partial retrieval failures; a wholly failed fetch is
not recorded as `NotFound`. Known canonical URLs and body hashes are excluded in add-evidence mode.
No automatic provider retries are performed. Job retry uses the existing saved request/checkpoints.

Source bundles retain request context, documents without full bodies, attempts, errors, warnings,
and provider-reported usage. Missing usage remains unknown. Read them with
`GET /api/research/jobs/{job_id}/sources` or `Database.source_bundles(job_id=...)`. Provider secrets
and response headers are not copied into this audit. Asynchronous provider jobs and PDF capture
are not implemented; the optional external job ID field is reserved for providers that supply one.

## Lab notebook

Ledger-native research creates an append-only lab notebook before retrieval begins. Plans, search
queries, candidate sources, failed attempts, provider-reported usage, and final findings are stored
as typed notebook entries. These entries document the search process but are not evidence: a claim
still requires a captured source and an exact supporting quote. Inspect sessions with
`epiq --project . notebook list` or the read-only FastAPI endpoints `GET /api/notebooks` and
`GET /api/notebooks/{session_id}`.

Interrupted commands intentionally leave their notebook active so another agent can inspect the
leads and continue the work. Close or pause it explicitly with `epiq notebook close`.

## Capture retention and review

Three separate layers appear in matrix lineage, the browser inspector, HTML/XLSX, and claims-long
exports:

1. Capture provenance: discovery, fetched, manual, or legacy; capture time/provider/content hash;
   and whether the stored excerpt was found in supplied source text.
2. Claim support: exact quotes and character offsets within the linked evidence excerpt.
3. Claim review: unreviewed, accepted, or rejected, with reviewer, reason, and event time.

By default, source bundles retain excerpts and hashes while full Markdown is discarded after
validation. Set `retain_content: true` to store immutable content-addressed text in the project's
SQLite database. Retained text is included in backups and `.epiq` project bundles. There is no
artifact deletion/retention scheduler.

Direct `evidence.add` batch operations may supply `provenance` with `capture_method`, `captured_at`,
`provider`, `content`, optional `content_hash`, and boolean `retain_content`. A fetched capture requires
content and a capture timestamp. `claim.assert.supports` accepts objects with `evidence_ref` (for
batch-local evidence) or `evidence_id`, `quote`, and optional `start_offset`/`end_offset`.
Invalid capture hashes or spans roll back the entire evidence-plus-claim batch.

Staged proposal and historical import documents are themselves immutable audit records. Documents
containing full capture text therefore require **explicit `retain_content: true`**, including while
pending review. An artifact row is created only on approval, but the proposal already retains its
input text. To verify and discard a full capture, use a direct batch write. Existing manually entered
excerpts remain supported and are marked unverified unless capture content is supplied.

Source-backed scalar research writes claims as unreviewed. Relationship proposals preserve the
capture and supporting spans through approval. To explicitly review an existing claim:

```python
import epiq

db = epiq.open("project.sqlite")
db.review_claim(claim_id, "accepted", "Checked the value against the source", actor="human:me")
report = db.provenance()
```

The inspector's **Mark reviewed** button records the same event through
`POST /api/claims/{claim_id}/review`. Batch `claim.review` also accepts `claim_id`, `decision`, and
`reason`. Review attribution is an audit label, not authenticated proof of a human identity. A
rejected review does not retract the asserted value; use retraction when that is intended. Historical
`known_at` projections show only reviews and support recorded by that cutoff.

`doctor.ok` covers structural integrity. Its separate `provenance` report lists discovery-only or
unverified evidence, missing support spans, and unreviewed/rejected active claims. A structurally
healthy database may still have provenance gaps; a clean provenance report is not a truth certificate.

## Validation

Offline fixtures exercise provider contracts, partial failures, cancellation, deduplication,
relationship approval, atomic provenance writes, historical reviews, and the browser review flow.
The live Firecrawl → EDSL smoke is opt-in because it consumes external services:

```bash
EPIQ_LIVE_SOURCE_SMOKE=1 .venv/bin/python -m pytest -q \
  tests/test_source_web.py::test_live_firecrawl_followed_by_edsl_remote_inference
```

It requires both provider credentials. Offline test success does not establish live account access,
model availability, or provider billing behavior.
