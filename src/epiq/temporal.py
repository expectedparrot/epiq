"""One interpretation of ledger dates and timezone-aware ISO timestamps."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from functools import lru_cache

from .errors import EpiqError

LATEST = "9999-12-31T23:59:59.999999Z"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)


@lru_cache(maxsize=8192)
def instant(value: str) -> str:
    """Return a sortable UTC instant; a calendar date means midnight UTC."""
    try:
        if not isinstance(value, str):
            raise ValueError
        if _DATE.fullmatch(value):
            parsed = datetime.fromisoformat(value).replace(tzinfo=UTC)
        elif _DATETIME.fullmatch(value):
            parsed = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        else:
            raise ValueError
        return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, TypeError) as error:
        raise EpiqError(
            "invalid_timestamp",
            f"Invalid ISO timestamp: {value!r}",
            "Use YYYY-MM-DD or a timezone-aware timestamp with at most six fractional digits.",
        ) from error


def normalize(value: str, *, field: str = "timestamp") -> str:
    """Validate dates without changing their display; normalize datetimes to UTC."""
    if not isinstance(value, str):
        raise EpiqError("invalid_timestamp", f"{field} must be an ISO date or timestamp")
    try:
        canonical = instant(value)
    except EpiqError as error:
        raise EpiqError(error.code, f"{field}: {error.message}", error.suggestion) from error
    return value if _DATE.fullmatch(value) else canonical


def research_instant(value: object) -> str | None:
    """Coerce a research date or ISO interval to one valid claim instant.

    Research providers sometimes report an observation period as ``start/end``. Claims require a
    single ``valid_from`` instant, so the interval end is the conservative point at which the
    period's result was fully observed.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    candidates = [value]
    if "/" in value:
        start, end = value.split("/", 1)
        candidates = [end, start]
    for candidate in candidates:
        try:
            return normalize(candidate.strip(), field="research date")
        except EpiqError:
            continue
    return None


def compare(left: str, right: str) -> int:
    """SQLite collation for old and new representations without rewriting history."""
    a, b = instant(left), instant(right)
    return (a > b) - (a < b)
