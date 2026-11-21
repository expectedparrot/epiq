"""Repeatable local performance measurements for Git-ledger projects."""

from __future__ import annotations

import sqlite3
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .ledger import LedgerRepository
from .ledger_projection import LedgerProjector


def benchmark(
    root: str | Path,
    *,
    iterations: int = 5,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """Measure ledger and disposable-projection operations without network access."""
    if iterations < 1 or iterations > 100:
        raise EpiqError("invalid_iterations", "Benchmark iterations must be between 1 and 100")
    repository = LedgerRepository(Path(root))
    projector = LedgerProjector(repository)

    validation = _samples(iterations, repository.validate, clock)
    cold_rebuild = _samples(iterations, lambda: projector.build(force=True), clock)
    incremental_open = _samples(iterations, projector.build, clock)
    projection_read = _samples(iterations, lambda: _read_projection(projector), clock)
    fingerprint = _samples(iterations, projector.semantic_fingerprint, clock)

    return {
        "root": str(repository.root),
        "iterations": iterations,
        "transactions": len(repository.transactions()),
        "operations": sum(len(item.operations) for item in repository.transactions()),
        "projection_bytes": projector.path.stat().st_size,
        "metrics": {
            "ledger_validate": _summary(validation),
            "cold_rebuild": _summary(cold_rebuild),
            "incremental_open": _summary(incremental_open),
            "projection_read": _summary(projection_read),
            "semantic_fingerprint": _summary(fingerprint),
        },
        "units": "milliseconds",
        "network_used": False,
    }


def _samples(
    iterations: int, operation: Callable[[], object], clock: Callable[[], int]
) -> list[float]:
    values: list[float] = []
    for _ in range(iterations):
        started = clock()
        operation()
        values.append((clock() - started) / 1_000_000)
    return values


def _summary(samples: list[float]) -> dict[str, Any]:
    return {
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
    }


def _read_projection(projector: LedgerProjector) -> None:
    with sqlite3.connect(projector.path) as connection:
        names = [
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type IN ('table','view') AND name LIKE 'projected_%'
                   ORDER BY name"""
            )
        ]
        for name in names:
            connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
