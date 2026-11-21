"""Versioned source-retrieval contracts (optional `sources` dependency group)."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import EpiqError
from .provenance import MAX_CONTENT, digest
from .temporal import instant

Progress = Callable[[str], None]
Cancelled = Callable[[], bool]


class SourceError(EpiqError):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False, retry_after: float | None = None
    ):
        super().__init__(code, message)
        self.retryable, self.retry_after = retryable, retry_after

    def as_dict(self):
        return super().as_dict() | {"retryable": self.retryable, "retry_after": self.retry_after}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def finite_json(self):
        try:
            serialized = json.dumps(self.model_dump(mode="json"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("Contracts require finite JSON values") from error
        if len(serialized) > 4_500_000:
            raise ValueError("Source contract exceeds the size limit")
        return self


class SourceCapabilities(Contract):
    search: bool
    fetch: bool
    full_text: bool = False
    markdown: bool = False
    pdf: bool = False
    domain_filters: bool = False
    date_filters: bool = False
    asynchronous: bool = False


class SourceRequest(Contract):
    schema_version: Literal["1"] = "1"
    request_id: str = Field(min_length=1, max_length=200)
    research_task_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=500)
    instructions: str = Field(default="", max_length=20_000)
    entity_context: dict[str, Any]
    question_context: dict[str, Any]
    max_sources: int = Field(default=5, ge=1, le=20)
    urls: list[str] = Field(default_factory=list, max_length=20)
    include_domains: list[str] = Field(default_factory=list, max_length=100)
    exclude_domains: list[str] = Field(default_factory=list, max_length=100)
    exclude_urls: list[str] = Field(default_factory=list, max_length=2000)
    exclude_hashes: list[str] = Field(default_factory=list, max_length=2000)
    published_after: date | None = None
    published_before: date | None = None
    capture: Literal["metadata", "excerpt", "markdown"] = "markdown"
    retain_content: bool = False

    @model_validator(mode="after")
    def bounded_dates(self):
        if (
            self.published_after
            and self.published_before
            and self.published_after > self.published_before
        ):
            raise ValueError("published_after cannot follow published_before")
        if len(json.dumps([self.entity_context, self.question_context])) > 100_000:
            raise ValueError("Research context exceeds the size limit")
        return self


class SourceDocument(Contract):
    source_ref: str = Field(min_length=1, max_length=200)
    requested_url: str | None = None
    canonical_url: str
    title: str = Field(max_length=2000)
    excerpt: str = Field(max_length=MAX_CONTENT)
    markdown: str | None = Field(default=None, max_length=MAX_CONTENT)
    published_at: date | None = None
    retrieved_at: datetime
    content_hash: str | None = None
    source_type: Literal["web", "report", "other"] = "web"
    provider: str = Field(min_length=1, max_length=200)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    capture_method: Literal["discovery", "fetched", "manual"] = "fetched"

    @field_validator("retrieved_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return datetime.fromisoformat(instant(value.isoformat()).replace("Z", "+00:00"))

    @field_validator("provider_metadata")
    @classmethod
    def metadata_has_no_credentials(cls, value):
        def inspect(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if key.lower().replace("-", "_") in {
                        "authorization",
                        "api_key",
                        "apikey",
                        "password",
                        "secret",
                        "token",
                        "access_token",
                        "cookie",
                        "set_cookie",
                        "headers",
                    }:
                        raise ValueError("Credentials and headers cannot be source metadata")
                    inspect(child)
            elif isinstance(item, list):
                for child in item:
                    inspect(child)

        inspect(value)
        if len(json.dumps(value)) > 10_000:
            raise ValueError("Provider metadata exceeds size limit")
        return value

    @model_validator(mode="after")
    def matching_capture(self):
        if self.capture_method == "discovery":
            if self.markdown is not None or self.content_hash is not None:
                raise ValueError("Discovery snippets cannot carry captured content")
        elif self.markdown is None or not self.markdown or self.excerpt not in self.markdown:
            raise ValueError("Captured sources require content containing the excerpt")
        elif self.content_hash and self.content_hash != digest(self.markdown):
            raise ValueError("Captured content hash mismatch")
        return self


class SourceAttempt(Contract):
    query: str = Field(max_length=2000)
    status: Literal["completed", "failed", "blocked", "empty", "cancelled"]
    detail: str = Field(default="", max_length=2000)
    started_at: datetime
    finished_at: datetime
    error_code: str | None = None
    retryable: bool = False
    retry_after: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def chronology(self):
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("Attempt times must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("Attempt finish precedes start")
        return self


class SourceBundle(Contract):
    schema_version: Literal["1"] = "1"
    request_id: str
    provider: str
    documents: list[SourceDocument] = Field(default_factory=list, max_length=20)
    attempts: list[SourceAttempt] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=100)
    external_job_id: str | None = None
    usage: dict[str, int | float | str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_refs(self):
        refs = [doc.source_ref for doc in self.documents]
        if len(refs) != len(set(refs)):
            raise ValueError("Source refs must be unique within a bundle")
        return self


class ResearchFinding(Contract):
    entity_id: str
    status: Literal["answered", "not_found"]
    value_json: str = Field(max_length=20_000)
    source_refs: list[str] = Field(max_length=20)
    support_quotes: dict[str, str] = Field(default_factory=dict)
    confidence: Literal["low", "medium", "high"] = "medium"
    observed_as_of: date | None = None
    notes: str = Field(default="", max_length=5000)


class SourceProvider(Protocol):
    name: str
    capabilities: SourceCapabilities

    def collect(
        self,
        request: SourceRequest,
        progress: Progress | None = None,
        cancelled: Cancelled | None = None,
    ) -> SourceBundle: ...


def check_capabilities(provider: SourceProvider, request: SourceRequest):
    capabilities = provider.capabilities
    requirements = {
        "fetch": bool(request.urls) or request.capture != "metadata",
        "search": not bool(request.urls),
        "markdown": request.capture == "markdown",
        "domain_filters": bool(request.include_domains or request.exclude_domains),
        "date_filters": bool(request.published_after or request.published_before),
    }
    for capability, required in requirements.items():
        if required and not getattr(capabilities, capability):
            raise SourceError(
                "unsupported_capability", f"{provider.name} does not support {capability}"
            )


def check_cancelled(cancelled: Cancelled | None):
    if cancelled and cancelled():
        raise SourceError("cancelled", "Source retrieval was cancelled")
