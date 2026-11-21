"""Repeatable local performance measurements for Git-ledger projects."""

from __future__ import annotations

import sqlite3
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .ledger import LedgerRepository, Operation, Transaction
from .ledger_projection import LedgerProjector

SYNTHETIC_PROFILES = {"small": 100, "medium": 1_000, "large": 10_000}


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


def benchmark_synthetic(
    profile: str,
    *,
    iterations: int = 3,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """Generate and measure a deterministic-shape ledger in temporary storage."""
    if profile not in SYNTHETIC_PROFILES:
        raise EpiqError("invalid_benchmark_profile", f"Unknown benchmark profile: {profile}")
    with tempfile.TemporaryDirectory(prefix=f"epiq-benchmark-{profile}-") as directory:
        root = Path(directory) / "project"
        started = clock()
        _generate(root, SYNTHETIC_PROFILES[profile])
        generation_ms = (clock() - started) / 1_000_000
        result = benchmark(root, iterations=iterations, clock=clock)
    result.update(
        {
            "root": None,
            "synthetic_profile": profile,
            "target_transactions": SYNTHETIC_PROFILES[profile],
            "generation_ms": round(generation_ms, 3),
            "ephemeral": True,
        }
    )
    return result


def assess_thresholds(
    result: dict[str, Any], thresholds: dict[str, float]
) -> dict[str, Any]:
    """Compare median timings with explicit environment-specific ceilings."""
    metrics = result["metrics"]
    unknown = sorted(set(thresholds) - set(metrics))
    invalid = [
        key
        for key, value in thresholds.items()
        if not isinstance(value, (int, float)) or value <= 0
    ]
    if unknown or invalid:
        raise EpiqError(
            "invalid_benchmark_thresholds",
            f"Unknown metrics={unknown} invalid ceilings={sorted(invalid)}",
        )
    checks = {
        key: {
            "median_ms": metrics[key]["median_ms"],
            "maximum_ms": maximum,
            "passed": metrics[key]["median_ms"] <= maximum,
        }
        for key, maximum in thresholds.items()
    }
    return {
        "passed": all(item["passed"] for item in checks.values()),
        "checks": checks,
    }


def _generate(root: Path, transaction_count: int) -> None:
    repository = LedgerRepository.initialize(
        root,
        f"Synthetic {transaction_count}",
        actor="benchmark:generator",
        recorded_at="2026-01-01T00:00:00Z",
        project_id=f"prj_benchmark_{transaction_count}",
        replica_id="rep_benchmark",
    )
    parent = repository.frontier()[0]
    for index in range(1, transaction_count):
        operation = (
            Operation.create(
                "entity_kind.define",
                {"kind": "BenchmarkEntity"},
                operation_id=_identifier("op", index),
            )
            if index == 1
            else Operation.create(
                "entity.create",
                {
                    "entity_id": f"ent_benchmark_{index:08d}",
                    "kind": "BenchmarkEntity",
                    "name": f"Entity {index:08d}",
                    "attributes": {"index": index, "group": index % 100},
                },
                operation_id=_identifier("op", index),
            )
        )
        transaction = Transaction.create(
            repository.config.project_id,
            repository.config.replica_id,
            "benchmark:generator",
            [operation],
            transaction_id=_identifier("txn", index),
            recorded_at="2026-01-01T00:00:01Z",
            parents=(parent,),
        )
        path = root / transaction.relative_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(transaction.canonical())
        parent = transaction.transaction_id


def _identifier(prefix: str, value: int) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    digits = ""
    remaining = value
    for _ in range(26):
        digits = alphabet[remaining & 31] + digits
        remaining >>= 5
    return f"{prefix}_{digits}"


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
