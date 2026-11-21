"""Bounded HTTP reads pinned to validated public addresses, including redirects."""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from .source_contracts import SourceError


@dataclass(frozen=True)
class HTTPResult:
    status: int
    url: str
    headers: dict[str, str]
    body: bytes


def public_url(url: str):
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise SourceError("request_failed", "Invalid source URL") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise SourceError(
            "request_failed", "Sources require a public HTTP(S) URL without credentials"
        )
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise SourceError("request_failed", "Private source URLs are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise SourceError("request_failed", "Private source addresses are not allowed")
    try:
        port = parts.port
    except ValueError as error:
        raise SourceError("request_failed", "Invalid source port") from error
    if port not in {None, 80, 443}:
        raise SourceError("request_failed", "Source URL must use a standard HTTP(S) port")
    return parts


def fetch_public(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    max_bytes: int = 4_500_000,
) -> HTTPResult:
    """No ambient proxies; validate DNS and connect to that exact address."""
    for _ in range(6):
        parts = public_url(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
            if not addresses or any(
                not ipaddress.ip_address(item[4][0]).is_global for item in addresses
            ):
                raise SourceError("request_failed", "Source resolved to a non-public address")
            sock = socket.create_connection((addresses[0][4][0], port), timeout=30)
            if parts.scheme == "https":
                try:
                    sock = ssl.create_default_context().wrap_socket(
                        sock, server_hostname=parts.hostname
                    )
                except BaseException:
                    sock.close()
                    raise
            connection = http.client.HTTPConnection(parts.hostname, port, timeout=30)
            connection.sock = sock
            try:
                connection.request(
                    method,
                    (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                    body=body,
                    headers={
                        "User-Agent": "Epiq/0.1 source-capture",
                        "Accept-Encoding": "identity",
                        **(headers or {}),
                    },
                )
                response = connection.getresponse()
                response_headers = {key.lower(): value for key, value in response.getheaders()}
                data = response.read(max_bytes + 1)
                status = response.status
            finally:
                connection.close()
            if len(data) > max_bytes:
                raise SourceError("invalid_response", "Source response exceeds the size limit")
            if status in {301, 302, 303, 307, 308}:
                if method != "GET" or headers and "Authorization" in headers:
                    raise SourceError(
                        "request_failed", "Authenticated provider redirects are disabled"
                    )
                location = response_headers.get("location")
                if not location:
                    raise SourceError("invalid_response", "Redirect has no location")
                url = urljoin(url, location)
                continue
            return HTTPResult(status, url, response_headers, data)
        except (OSError, http.client.HTTPException, UnicodeError) as error:
            raise SourceError(
                "request_failed", "Public source request failed", retryable=True
            ) from error
    raise SourceError("request_failed", "Too many source redirects")


def check_status(response: HTTPResult) -> None:
    if response.status in {401, 403}:
        raise SourceError("authentication_failed", "Provider authentication failed")
    if response.status == 429:
        try:
            delay = max(0, float(response.headers.get("retry-after", "0")))
            if not math.isfinite(delay):
                delay = None
        except ValueError:
            delay = None
        raise SourceError(
            "rate_limited", "Provider rate limit reached", retryable=True, retry_after=delay
        )
    if response.status < 200 or response.status >= 300:
        raise SourceError(
            "request_failed",
            f"Provider returned HTTP {response.status}",
            retryable=response.status >= 500,
        )


def checked_json(response: HTTPResult) -> dict:
    check_status(response)

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    try:
        data = json.loads(response.body, parse_constant=reject_constant)
    except (ValueError, UnicodeError) as error:
        raise SourceError("invalid_response", "Provider returned invalid JSON") from error
    if not isinstance(data, dict) or data.get("success") is False:
        raise SourceError("invalid_response", "Provider did not return a successful object")
    return data
