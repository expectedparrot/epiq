import subprocess
from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.ledger_api import LedgerDatabase
from epiq.ledger_git import GitTransport


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_git_transport_publishes_only_epiq_owned_paths_and_clones(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    root = tmp_path / "writer"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Epiq Test")
    git(root, "config", "user.email", "epiq@example.test")
    git(root, "remote", "add", "origin", str(remote))
    database = LedgerDatabase(
        root,
        create=True,
        name="Remote project",
        actor="human:test",
        project_id="prj_remote",
        replica_id="rep_remote",
        clock=lambda: "2026-11-21T12:00:00Z",
    )
    (root / "unrelated.txt").write_text("do not commit")

    published = GitTransport(root).publish(message="Initialize Epiq project")

    assert published["committed"] is True
    assert published["pushed"] is True
    assert "unrelated.txt" not in git(root, "show", "--name-only", "--format=")
    assert "?? unrelated.txt" in git(root, "status", "--porcelain")
    assert GitTransport(root).status()["upstream"] == "origin/main"

    clone = tmp_path / "reader"
    cloned = GitTransport.clone(str(remote), clone)
    assert cloned["project_id"] == "prj_remote"
    assert cloned["healthy"] is True
    assert (clone / ".epiq" / "projection.sqlite").exists()

    database.define_kind("Company", actor="agent:test")
    fetched = GitTransport(root).fetch()
    assert fetched["fetched"] is True


def test_git_transport_refuses_unrelated_staged_changes(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    root = tmp_path / "writer"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Epiq Test")
    git(root, "config", "user.email", "epiq@example.test")
    git(root, "remote", "add", "origin", str(remote))
    LedgerDatabase(root, create=True, name="Remote project", actor="human:test")
    (root / "unrelated.txt").write_text("already staged")
    git(root, "add", "unrelated.txt")

    with pytest.raises(EpiqError) as error:
        GitTransport(root).publish(message="Do not include unrelated work")

    assert error.value.code == "unrelated_staged_changes"
    assert "A  unrelated.txt" in git(root, "status", "--porcelain")
    assert git(remote, "for-each-ref", "--format=%(refname)", "refs/heads") == ""
