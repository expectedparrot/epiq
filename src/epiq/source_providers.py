"""Replaceable, bounded discovery and capture providers."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from .provenance import MAX_CONTENT, digest
from .source_contracts import (
    Cancelled,
    Progress,
    SourceAttempt,
    SourceBundle,
    SourceCapabilities,
    SourceDocument,
    SourceError,
    SourceProvider,
    SourceRequest,
    check_cancelled,
    check_capabilities,
)
from .source_http import check_status, checked_json, fetch_public, public_url
from .store import canonicalize_url


def now():
    return datetime.now(UTC)


def allowed_url(url: str, request: SourceRequest):
    parts = public_url(url)
    host = parts.hostname.lower().rstrip(".")

    def matches(domain):
        domain = domain.lower().strip().rstrip(".")
        return host == domain or host.endswith("." + domain)

    return (
        canonicalize_url(url) not in {canonicalize_url(u) for u in request.exclude_urls}
        and (not request.include_domains or any(matches(d) for d in request.include_domains))
        and not any(matches(d) for d in request.exclude_domains)
    )


def normalize_bundle(bundle: SourceBundle | dict, request: SourceRequest) -> SourceBundle:
    try:
        bundle = SourceBundle.model_validate(
            bundle.model_dump(mode="json") if isinstance(bundle, SourceBundle) else bundle
        )
    except ValidationError as error:
        raise SourceError(
            "invalid_response", "Provider returned a malformed source bundle"
        ) from error
    if bundle.request_id != request.request_id:
        raise SourceError("invalid_response", "Provider bundle belongs to a different request")
    urls, hashes, documents = set(), set(request.exclude_hashes), []
    for doc in bundle.documents:
        url = canonicalize_url(doc.canonical_url)
        content_hash = digest(doc.markdown) if doc.markdown is not None else None
        if not allowed_url(url, request) or url in urls or content_hash and content_hash in hashes:
            continue
        if request.published_after and (
            doc.published_at is None or doc.published_at < request.published_after
        ):
            continue
        if request.published_before and (
            doc.published_at is None or doc.published_at > request.published_before
        ):
            continue
        urls.add(url)
        if content_hash:
            hashes.add(content_hash)
        documents.append(
            doc.model_copy(update={"canonical_url": url, "content_hash": content_hash})
        )
    return bundle.model_copy(update={"documents": documents[: request.max_sources]})


def attempt(query: str, started: datetime, error: SourceError | None = None, *, empty=False):
    return SourceAttempt(
        query=query,
        status=(
            "cancelled"
            if error and error.code == "cancelled"
            else "failed"
            if error
            else "empty"
            if empty
            else "completed"
        ),
        detail=error.message if error else "",
        started_at=started,
        finished_at=now(),
        error_code=error.code if error else None,
        retryable=error.retryable if error else False,
        retry_after=error.retry_after if error else None,
    )


class ProviderRegistry:
    def __init__(self):
        self.factories: dict[str, Callable[..., SourceProvider]] = {}

    def register(self, name: str, factory: Callable[..., SourceProvider]):
        self.factories[name] = factory

    def create(self, name: str, **kwargs) -> SourceProvider:
        if name not in self.factories:
            raise SourceError("provider_not_configured", f"Unknown source provider: {name}")
        return self.factories[name](**kwargs)


class FixtureSourceProvider:
    name = "fixture"
    capabilities = SourceCapabilities(
        search=True,
        fetch=True,
        full_text=True,
        markdown=True,
        domain_filters=True,
        date_filters=True,
    )

    def __init__(self, bundles: dict[str, SourceBundle] | Callable[[SourceRequest], SourceBundle]):
        self.bundles = bundles

    def collect(self, request, progress=None, cancelled=None):
        check_capabilities(self, request)
        check_cancelled(cancelled)
        bundle = (
            self.bundles(request) if callable(self.bundles) else self.bundles[request.request_id]
        )
        return normalize_bundle(bundle, request)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"} and not self.skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class URLSourceProvider:
    name = "url"
    capabilities = SourceCapabilities(
        search=False, fetch=True, full_text=True, markdown=True, domain_filters=True
    )

    def __init__(self, transport=fetch_public):
        self.transport = transport

    def collect(
        self,
        request: SourceRequest,
        progress: Progress | None = None,
        cancelled: Cancelled | None = None,
    ) -> SourceBundle:
        check_capabilities(self, request)
        documents, attempts = [], []
        for url in request.urls:
            check_cancelled(cancelled)
            started = now()
            try:
                if not allowed_url(url, request):
                    continue
                if progress:
                    progress(f"Fetching {urlsplit(url).hostname}")
                response = self.transport(url)
                check_cancelled(cancelled)
                check_status(response)
                if not allowed_url(response.url, request):
                    raise SourceError("request_failed", "Redirected source is excluded")
                content_type = response.headers.get("content-type", "").split(";")[0].lower()
                if content_type not in {
                    "text/html",
                    "text/plain",
                    "text/markdown",
                    "application/xhtml+xml",
                }:
                    raise SourceError(
                        "unsupported_capability", "URL provider supports HTML and text captures"
                    )
                content = response.body.decode("utf-8", errors="replace")
                if "html" in content_type:
                    parser = _HTMLText()
                    parser.feed(content)
                    content = "".join(parser.parts).strip()
                if not content or len(content) > MAX_CONTENT:
                    raise SourceError(
                        "invalid_response", "Source text is empty or exceeds the capture limit"
                    )
                documents.append(
                    SourceDocument(
                        source_ref=f"url-{len(documents)}",
                        requested_url=url,
                        canonical_url=response.url,
                        title=url,
                        excerpt=content[:2000],
                        markdown=content,
                        retrieved_at=now(),
                        provider=self.name,
                    )
                )
                attempts.append(attempt(url, started))
            except SourceError as error:
                if error.code == "cancelled":
                    raise
                attempts.append(attempt(url, started, error))
            if len(documents) >= request.max_sources:
                break
        return normalize_bundle(
            SourceBundle(
                request_id=request.request_id,
                provider=self.name,
                documents=documents,
                attempts=attempts,
            ),
            request,
        )


def record_credits(usage: dict, key: str, response: dict) -> None:
    # Missing usage is unknown; do not invent a cost per request.
    credits = response.get("creditsUsed")
    if type(credits) in {int, float} and math.isfinite(credits) and credits >= 0:
        usage[key] = usage.get(key, 0) + credits


class FirecrawlSourceProvider:
    name = "firecrawl"
    capabilities = SourceCapabilities(
        search=True,
        fetch=True,
        full_text=True,
        markdown=True,
        domain_filters=True,
        date_filters=True,
    )

    def __init__(
        self,
        api_key: str | None = None,
        transport=fetch_public,
        endpoint="https://api.firecrawl.dev/v2",
    ):
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY")
        self.transport, self.endpoint = transport, endpoint.rstrip("/")

    def _call(self, operation: str, payload: dict) -> dict:
        if not self.api_key:
            raise SourceError("provider_not_configured", "FIRECRAWL_API_KEY is not configured")
        return checked_json(
            self.transport(
                self.endpoint + "/" + operation,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                },
                body=json.dumps(payload).encode(),
            )
        )

    def collect(
        self,
        request: SourceRequest,
        progress: Progress | None = None,
        cancelled: Cancelled | None = None,
    ) -> SourceBundle:
        check_capabilities(self, request)
        check_cancelled(cancelled)
        attempts, documents, usage = [], [], {}
        hits = [{"url": url} for url in request.urls]
        if not hits:
            started = now()
            payload: dict[str, Any] = {
                "query": request.query,
                "limit": request.max_sources,
                "sources": ["web"],
                "includeDomains": request.include_domains,
                "excludeDomains": request.exclude_domains,
            }
            if request.published_after or request.published_before:
                bounds = ["cdr:1"]
                if request.published_after:
                    bounds.append("cd_min:" + request.published_after.strftime("%m/%d/%Y"))
                if request.published_before:
                    bounds.append("cd_max:" + request.published_before.strftime("%m/%d/%Y"))
                payload["tbs"] = ",".join(bounds)
            try:
                if progress:
                    progress("Searching with Firecrawl")
                response = self._call("search", payload)
                data = response.get("data")
                hits = data.get("web") if isinstance(data, dict) else None
                if not isinstance(hits, list) or len(hits) > 100:
                    raise SourceError(
                        "invalid_response", "Firecrawl search returned invalid results"
                    )
                record_credits(usage, "search_credits", response)
                attempts.append(attempt(request.query, started, empty=not hits))
            except SourceError as error:
                if error.code in {"provider_not_configured", "authentication_failed"}:
                    raise
                attempts.append(attempt(request.query, started, error))
                hits = []
        seen = set()
        for hit in hits:
            check_cancelled(cancelled)
            started = now()
            url = hit.get("url", "") if isinstance(hit, dict) else ""
            try:
                if not allowed_url(url, request) or canonicalize_url(url) in seen:
                    continue
                seen.add(canonicalize_url(url))
                if request.capture == "metadata":
                    documents.append(
                        SourceDocument(
                            source_ref=f"fc-{len(documents)}",
                            canonical_url=url,
                            title=hit.get("title", url),
                            excerpt=hit.get("description", ""),
                            retrieved_at=now(),
                            provider=self.name,
                            capture_method="discovery",
                        )
                    )
                    continue
                if progress:
                    progress(f"Capturing {urlsplit(url).hostname} with Firecrawl")
                response = self._call(
                    "scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": True}
                )
                data = response.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("markdown"), str):
                    raise SourceError("invalid_response", "Firecrawl scrape returned no Markdown")
                metadata = data.get("metadata") or {}
                if not isinstance(metadata, dict):
                    raise SourceError("invalid_response", "Invalid capture metadata")
                published = metadata.get("publishedTime") or metadata.get("publishedDate")
                try:
                    published = date.fromisoformat(published[:10]) if published else None
                except (ValueError, TypeError):
                    published = None
                content = data["markdown"]
                documents.append(
                    SourceDocument(
                        source_ref=f"fc-{len(documents)}",
                        requested_url=url,
                        canonical_url=metadata.get("sourceURL") or metadata.get("url") or url,
                        title=metadata.get("title") or hit.get("title") or url,
                        excerpt=content[:2000],
                        markdown=content,
                        retrieved_at=now(),
                        published_at=published,
                        provider=self.name,
                        provider_metadata={"status_code": metadata.get("statusCode", 200)},
                    )
                )
                record_credits(usage, "scrape_credits", response)
                attempts.append(attempt(url, started))
            except ValidationError:
                attempts.append(
                    attempt(
                        url, started, SourceError("invalid_response", "Invalid captured document")
                    )
                )
            except SourceError as error:
                if error.code == "cancelled":
                    raise
                attempts.append(attempt(url, started, error))
                if error.code in {"rate_limited", "authentication_failed"}:
                    break
            if len(documents) >= request.max_sources:
                break
        check_cancelled(cancelled)
        return normalize_bundle(
            SourceBundle(
                request_id=request.request_id,
                provider=self.name,
                documents=documents,
                attempts=attempts,
                usage=usage,
            ),
            request,
        )


class CompositeSourceProvider:
    name = "composite"

    def __init__(self, discovery: SourceProvider, capture: SourceProvider):
        self.discovery, self.capture = discovery, capture
        self.capabilities = SourceCapabilities(
            search=discovery.capabilities.search,
            fetch=capture.capabilities.fetch,
            full_text=capture.capabilities.full_text,
            markdown=capture.capabilities.markdown,
            domain_filters=discovery.capabilities.domain_filters
            and capture.capabilities.domain_filters,
            date_filters=False,
        )

    def collect(self, request, progress=None, cancelled=None):
        check_capabilities(self, request)
        if request.urls:
            return self.capture.collect(request, progress, cancelled)
        found = self.discovery.collect(
            request.model_copy(update={"capture": "metadata"}), progress, cancelled
        )
        found = normalize_bundle(found, request)
        if not found.documents or request.capture == "metadata":
            return found
        captured = self.capture.collect(
            request.model_copy(
                update={
                    "urls": [doc.canonical_url for doc in found.documents],
                }
            ),
            progress,
            cancelled,
        )
        return normalize_bundle(
            SourceBundle(
                request_id=request.request_id,
                provider=self.name,
                documents=captured.documents,
                attempts=[*found.attempts, *captured.attempts],
                warnings=[*found.warnings, *captured.warnings],
                usage={
                    **{"discovery_" + k: v for k, v in found.usage.items()},
                    **{"capture_" + k: v for k, v in captured.usage.items()},
                },
            ),
            request,
        )


registry = ProviderRegistry()
registry.register("firecrawl", FirecrawlSourceProvider)
registry.register("url", URLSourceProvider)
registry.register("fixture", FixtureSourceProvider)
