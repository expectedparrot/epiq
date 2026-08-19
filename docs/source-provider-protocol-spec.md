# Source Provider Protocol

**Status:** Proposed

**Target:** Epiq 0.2

**Purpose:** Make web discovery and source capture replaceable independently of model reasoning.

## Summary

Epiq currently asks a `ResearchRunner` to both find sources and interpret them. That makes
retrieval, reasoning, citations, and model execution one opaque operation. It also prevents Epiq
from combining a durable web-data service such as Firecrawl with an execution engine such as EDSL.

This specification introduces a `SourceProvider` protocol. A provider receives a bounded,
context-rich source request and returns immutable source documents plus an audit of what it tried.
A separate `ResearchRunner` interprets those documents and returns typed findings that cite only
the source IDs supplied by Epiq. Epiq remains the sole component allowed to validate and write
evidence or claims.

```text
ResearchTask
    -> SourceProvider.collect(SourceRequest)
    -> SourceBundle
    -> ResearchRunner.run(ResearchTask, SourceBundle)
    -> ResearchFinding[]
    -> Epiq validation and write-back
```

The first production provider should be Firecrawl. Deterministic fixtures, explicit-URL fetching,
and provider composition are also required so the abstraction is testable and not Firecrawl-shaped.

## Goals

- Replace source discovery and capture without changing database or UI semantics.
- Preserve project, table, entity, and question context during retrieval.
- Capture enough provenance to reproduce, deduplicate, and audit research.
- Prevent reasoning models from introducing uncaptured or invented citations.
- Support search-only, fetch-only, and combined providers through one stable contract.
- Preserve current OpenAI and Codex runners during an incremental migration.

## Non-goals

- Letting a provider write directly to the Epiq database.
- Defining a general browser automation protocol in v1.
- Requiring every source provider to return full text.
- Replacing the `ResearchRunner` abstraction or choosing one mandatory reasoning engine.
- Storing API credentials in an Epiq project database.

## Public contracts

The contracts are versioned Pydantic models so they can cross process boundaries and be recorded
with a research run. Dates and timestamps use ISO 8601; timestamps are UTC.

### Capabilities

```python
class SourceCapabilities(BaseModel):
    search: bool
    fetch: bool
    full_text: bool = False
    markdown: bool = False
    pdf: bool = False
    domain_filters: bool = False
    date_filters: bool = False
    asynchronous: bool = False
```

Capabilities are descriptive and validated before dispatch. Unsupported request options fail
before external work begins; they are never silently ignored.

### Request

```python
class SourceRequest(BaseModel):
    schema_version: Literal["1"] = "1"
    request_id: str
    research_task_id: str
    query: str
    instructions: str = ""
    entity_context: dict[str, Any]
    question_context: dict[str, Any]
    max_sources: int = Field(default=5, ge=1, le=20)
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    exclude_urls: list[str] = Field(default_factory=list)
    published_after: date | None = None
    published_before: date | None = None
    capture: Literal["metadata", "excerpt", "markdown"] = "markdown"
```

`entity_context` contains the stable entity ID, canonical name, aliases, attributes, project name,
table description, and relevant peer-row context. `question_context` contains the versioned
question ID, value type, definition, volatility, research guidance, and task mode. These are
identity constraints, not optional prompt decoration.

`exclude_urls` contains every canonical URL already attached to the cell. This makes “get more
evidence” a provider-level invariant rather than a prompt suggestion.

### Documents and attempts

```python
class SourceDocument(BaseModel):
    source_ref: str
    requested_url: str | None = None
    canonical_url: str
    title: str
    excerpt: str
    markdown: str | None = None
    published_at: date | None = None
    retrieved_at: datetime
    content_hash: str | None = None
    source_type: Literal["web", "report", "other"] = "web"
    provider: str
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class SourceAttempt(BaseModel):
    query: str
    status: Literal["completed", "failed", "blocked", "empty"]
    detail: str = ""
    started_at: datetime
    finished_at: datetime


class SourceBundle(BaseModel):
    schema_version: Literal["1"] = "1"
    request_id: str
    provider: str
    documents: list[SourceDocument]
    attempts: list[SourceAttempt]
    warnings: list[str] = Field(default_factory=list)
    external_job_id: str | None = None
    usage: dict[str, int | float | str] = Field(default_factory=dict)
```

`source_ref` is unique within the bundle. It is not yet a durable Epiq evidence ID. Epiq
canonicalizes URLs and verifies or computes content hashes after receiving the bundle.

### Provider

```python
Progress = Callable[[str], None]
Cancelled = Callable[[], bool]


class SourceProvider(Protocol):
    name: str
    capabilities: SourceCapabilities

    def collect(
        self,
        request: SourceRequest,
        progress: Progress | None = None,
        cancelled: Cancelled | None = None,
    ) -> SourceBundle: ...
```

The callable is synchronous to match the current background-thread orchestration. Providers may
wrap asynchronous external jobs internally and must poll cancellation between network operations.
A later async protocol may be added without changing the serialized contracts.

## Provider behavior

Every provider must:

1. Use entity and question context to disambiguate the intended subject.
2. Canonicalize or preserve enough URL information for Epiq to canonicalize it.
3. Exclude known URLs before returning results.
4. Return only material actually retrieved from the reported URL.
5. Record failed, blocked, and empty attempts instead of representing them as evidence.
6. Return partial successful results when some fetches fail.
7. Avoid interpreting a source into an Epiq value or claim.
8. Never write to the Epiq database.

Provider failures use stable codes: `provider_not_configured`, `unsupported_capability`,
`authentication_failed`, `rate_limited`, `request_failed`, `cancelled`, and `invalid_response`.
Errors may include retryability and an optional retry-after value. Credentials are read from the
server environment or an injected secret provider and are never serialized into a task or bundle.

## Initial implementations

### `FirecrawlSourceProvider`

- Uses Firecrawl Search for discovery and Scrape for Markdown capture.
- Reads `FIRECRAWL_API_KEY` from the environment unless explicitly injected.
- Applies domain and date filters when supported.
- Returns Firecrawl job IDs and credit usage as provider metadata.
- Does not use Firecrawl Agent to interpret Epiq values in v1.

### `URLSourceProvider`

- Fetches explicit URLs supplied in the request instructions or provider-specific options.
- Does not search.
- Provides a small deterministic path for known-source workflows.

### `FixtureSourceProvider`

- Returns predefined bundles with no network calls.
- Is used by protocol, orchestration, write-back, and error-path tests.

### `CompositeSourceProvider`

- Delegates discovery and capture to configured providers.
- A typical composition may search with one provider and capture with Firecrawl.
- Deduplicates by canonical URL and content hash while retaining provider metadata.

OpenAI hosted web search remains a legacy combined research backend initially because it performs
retrieval and interpretation in one model call. It may later implement `SourceProvider` when its
adapter can expose captured search results independently of the answer.

## Reasoning and citation contract

The research runner receives a `ResearchTask` and one or more `SourceBundle` objects. Its output
must reference captured sources rather than supply arbitrary provenance:

```python
class ResearchFinding(BaseModel):
    entity_id: str
    status: Literal["answered", "not_found"]
    value_json: str
    source_refs: list[str]
    confidence: Literal["low", "medium", "high"]
    observed_as_of: date | None = None
    notes: str = ""
```

For `answered`, every `source_ref` must resolve to a document supplied to that runner invocation.
For `not_found`, `source_refs` is empty and the run retains `SourceAttempt` records explaining the
search. A runner may select an excerpt from captured Markdown, but it may not change the source URL,
title, publication date, retrieval date, or hash.

## Orchestration and persistence

1. Epiq materializes the bounded target cells and creates `SourceRequest` objects.
2. The configured provider collects sources independently for each target entity.
3. Epiq emits provider progress into the existing research job activity stream.
4. The reasoning runner interprets each completed bundle. Bundles may be processed incrementally;
   a slow entity must not delay ready findings for other cells.
5. Epiq validates the finding value and all referenced source IDs.
6. Epiq persists source metadata, excerpt, content hash, and claim links transactionally.
7. Captured Markdown is retained as an optional immutable evidence artifact. The core evidence row
   stores its hash and artifact reference; deployments may disable full-text retention while still
   preserving the excerpt and hash.
8. `add_evidence` requests include existing canonical URLs in `exclude_urls` and attach newly
   accepted evidence to the existing claim without duplicating prior sources.

Research jobs record provider name, request and bundle schema versions, external job IDs, attempts,
usage, and terminal error codes. Raw credentials and provider authorization headers are excluded.

## Configuration

The application creates providers through a registry rather than branching on provider names:

```python
registry.register("firecrawl", FirecrawlSourceProvider)
registry.register("url", URLSourceProvider)
registry.register("fixture", FixtureSourceProvider)
```

`EPIQ_SOURCE_PROVIDER=firecrawl` selects the server default. `create_app(source_provider=...)`
continues to support dependency injection in tests and embedding applications. Project-level
provider selection may be added later, but v1 does not store credentials or provider secrets in
SQLite.

## Migration

1. Add the contracts, registry, fixture provider, and orchestration tests without changing the
   default runner.
2. Add Firecrawl and URL providers behind explicit configuration.
3. Add a source-bounded reasoning adapter, initially EDSL-compatible, that returns `source_refs`.
4. Enable the split pipeline as an opt-in backend and compare it against existing research tests.
5. Make the split pipeline the default only after source quality, incremental updates, cancellation,
   and add-evidence behavior pass end-to-end acceptance tests.
6. Retain current OpenAI and Codex combined runners as selectable compatibility backends.

## Test and acceptance criteria

- Contract models round-trip through JSON and reject unsupported or malformed values.
- Capability mismatch fails before a provider call.
- Context for ambiguous entities reaches the provider unchanged.
- Known and canonically equivalent URLs are excluded from add-evidence results.
- Duplicate URLs and duplicate content are returned once.
- Partial fetch failure preserves successful documents and records failed attempts.
- Cancellation stops further provider work and leaves the job terminally cancelled.
- A reasoning model cannot cite a source absent from its bundle.
- Findings from separate entities commit as soon as each is ready.
- Not-found outcomes retain attempted queries and failure details without creating evidence.
- Firecrawl HTTP tests cover search, scrape, pagination or async polling, rate limits, malformed
  responses, and credit metadata without live network access.
- A gated smoke test exercises Firecrawl retrieval followed by EDSL remote inference using separate
  `FIRECRAWL_API_KEY` and `EXPECTED_PARROT_API_KEY` credentials.
- Existing OpenAI, Codex, manual evidence, relationship proposal, and claim write-back tests remain
  green.

## Security and operational constraints

- Provider output is untrusted external input and is size-limited before persistence or prompting.
- Captured pages are treated as data, never executable instructions; reasoning prompts explicitly
  identify source text as untrusted.
- URLs use the existing canonicalization and SSRF protections before direct fetching.
- Full-text retention is private by default and must respect deployment retention policy.
- Provider rate limits and costs are surfaced in the research job rather than silently retried
  without bounds.
