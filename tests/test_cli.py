import json
from pathlib import Path

from epiq.cli import main


def test_cli_demo_round_trip(tmp_path: Path, capsys) -> None:
    database = tmp_path / "demo.sqlite"
    main(["--db", str(database), "init", "--name", "Demo"])
    assert json.loads(capsys.readouterr().out)["ok"] is True

    main(["--db", str(database), "demo", "patriots"])
    demo = json.loads(capsys.readouterr().out)
    assert demo["final"]["record"] == "14-3"

    main(["--db", str(database), "season-record", "New England Patriots 2025"])
    record = json.loads(capsys.readouterr().out)
    assert record["wins"] == 14
    assert len(record["lineage"]) == 17

    report = tmp_path / "report.html"
    main(["--db", str(database), "export-html", "--kind", "Game", "--output", str(report)])
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert "Patriots 2025 Week 1" in report.read_text()


def test_cli_can_remember_workspace_database(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "research" / "market.sqlite"

    main(["use", str(database)])
    selected = json.loads(capsys.readouterr().out)
    assert selected["exists"] is False

    main(["db"])
    current = json.loads(capsys.readouterr().out)
    assert current["database"] == str(database)
    assert current["source"] == "workspace"

    main(["init", "--name", "Market"])
    assert json.loads(capsys.readouterr().out)["database"] == str(database)
    assert database.exists()

    environment_database = tmp_path / "environment.sqlite"
    monkeypatch.setenv("EPIQ_DB", str(environment_database))
    main(["db"])
    current = json.loads(capsys.readouterr().out)
    assert current["database"] == str(environment_database)
    assert current["source"] == "environment"
