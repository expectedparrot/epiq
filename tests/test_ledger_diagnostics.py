from pathlib import Path

from epiq.ledger import LedgerRepository
from epiq.ledger_diagnostics import diagnose


def test_diagnostics_are_offline_and_rebuild_disposable_projection(tmp_path: Path) -> None:
    root = tmp_path / "project"
    repository = LedgerRepository.initialize(
        root,
        "Project",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_doctor",
        replica_id="rep_doctor",
    )
    projection = root / ".epiq" / "projection.sqlite"
    assert not projection.exists()

    result = diagnose(root, rebuild=True)

    assert result["healthy"] is True
    assert result["clean"] is True
    assert result["build_mode"] == "rebuild"
    assert result["projection"]["verified"] is True
    assert result["projection"]["semantic_fingerprint"].startswith("sha256:")
    assert projection.exists()
    assert repository.validate()["transactions"] == 1
