import json
from pathlib import Path

from epiq.cli import main


def data(capsys) -> dict:
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    return payload["data"]


def test_ledger_cli_initializes_validates_and_rebuilds(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "--actor",
            "human:test",
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--project-id",
            "prj_cli",
            "--replica-id",
            "rep_cli",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    initialized = data(capsys)
    assert initialized["ledger"]["transactions"] == 1
    assert initialized["projection"]["mode"] == "rebuild"

    main(["ledger", "validate", "--path", str(root)])
    assert data(capsys)["operations"] == 1

    main(["ledger", "status", "--path", str(root)])
    assert data(capsys)["state"] == "ready"

    (root / ".epiq" / "projection.sqlite").unlink()
    main(["ledger", "build", "--path", str(root), "--verify"])
    rebuilt = data(capsys)
    assert rebuilt["verified"] is True
    assert rebuilt["transactions"] == 1


def test_ledger_cli_command_name_includes_nested_action(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "epiq ledger init"
