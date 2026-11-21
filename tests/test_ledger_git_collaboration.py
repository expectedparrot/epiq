import subprocess
from pathlib import Path

from epiq.ledger_api import LedgerDatabase
from epiq.ledger_diagnostics import diagnose


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_independent_agent_branches_merge_as_transaction_set(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Epiq Test")
    git(root, "config", "user.email", "epiq@example.test")
    database = LedgerDatabase(
        root,
        create=True,
        name="Collaboration",
        actor="human:test",
        project_id="prj_collaboration",
        replica_id="rep_collaboration",
        clock=lambda: "2026-11-21T12:00:00Z",
    )
    database.define_kind(
        "Company",
        actor="human:test",
        recorded_at="2026-11-21T12:01:00Z",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "initialize project")

    for branch, actor, name in (
        ("agent-a", "agent:a", "Acme"),
        ("agent-b", "agent:b", "ACME"),
    ):
        git(root, "checkout", "main")
        git(root, "checkout", "-b", branch)
        LedgerDatabase(root).create_entity(
            "Company",
            name,
            actor=actor,
            recorded_at="2026-11-21T12:02:00Z",
            idempotency_key="discover-acme",
        )
        git(root, "add", ".")
        git(root, "commit", "-m", f"{actor} discovers company")

    git(root, "checkout", "main")
    git(root, "merge", "--no-edit", "agent-a")
    git(root, "merge", "--no-edit", "agent-b")

    result = diagnose(root, rebuild=True)
    assert result["healthy"] is True
    assert result["clean"] is False
    assert result["ledger"]["transactions"] == 4
    assert [item["conflict_type"] for item in result["conflicts"]] == [
        "entity_identity"
    ]
    assert git(root, "status", "--porcelain") == ""
