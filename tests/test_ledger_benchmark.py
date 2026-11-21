from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.ledger import LedgerRepository
from epiq.ledger_benchmark import assess_thresholds, benchmark, benchmark_synthetic


def test_benchmark_measures_local_ledger_and_projection_operations(tmp_path: Path) -> None:
    root = tmp_path / "project"
    LedgerRepository.initialize(
        root,
        "Benchmark",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_benchmark",
        replica_id="rep_benchmark",
    )

    result = benchmark(root, iterations=2)

    assert result["iterations"] == 2
    assert result["transactions"] == 1
    assert result["operations"] == 1
    assert result["projection_bytes"] > 0
    assert result["network_used"] is False
    assert set(result["metrics"]) == {
        "ledger_validate",
        "cold_rebuild",
        "incremental_open",
        "projection_read",
        "semantic_fingerprint",
    }
    assert all(len(metric["samples_ms"]) == 2 for metric in result["metrics"].values())


def test_benchmark_rejects_unbounded_iterations(tmp_path: Path) -> None:
    root = tmp_path / "project"
    LedgerRepository.initialize(
        root,
        "Benchmark",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
    )

    with pytest.raises(EpiqError) as error:
        benchmark(root, iterations=0)
    assert error.value.code == "invalid_iterations"


def test_synthetic_profile_and_threshold_assessment() -> None:
    result = benchmark_synthetic("small", iterations=1)

    assert result["synthetic_profile"] == "small"
    assert result["transactions"] == 100
    assert result["ephemeral"] is True
    assessment = assess_thresholds(result, {"cold_rebuild": 60_000})
    assert assessment["passed"] is True
