"""Git subprocess transport for ledger-backed Epiq projects."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .ledger import LedgerRepository
from .ledger_diagnostics import diagnose

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")


class GitTransport:
    """Communicate ledger files through Git without making Git the domain model."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        if not (self.root / ".git").exists():
            raise EpiqError("not_git_repository", f"Not a Git repository: {self.root}")

    @classmethod
    def clone(cls, url: str, destination: str | Path) -> dict[str, Any]:
        if not url or url.startswith("-"):
            raise EpiqError("invalid_git_remote", "Git remote URL is invalid")
        target = Path(destination).expanduser().resolve()
        cls._run_at(target.parent, "git", "clone", "--", url, str(target))
        repository = LedgerRepository(target)
        health = diagnose(target, rebuild=True)
        return {
            "root": str(target),
            "remote": url,
            "project_id": repository.config.project_id,
            "healthy": health["healthy"],
            "transactions": health["ledger"]["transactions"],
        }

    def status(self, remote: str = "origin") -> dict[str, Any]:
        self._name(remote, "remote")
        branch = self._git("branch", "--show-current")
        url = self._git("remote", "get-url", remote)
        dirty = self._git("status", "--porcelain").splitlines()
        upstream = self._git_optional("rev-parse", "--abbrev-ref", "@{upstream}")
        ahead = behind = None
        if upstream:
            counts = self._git("rev-list", "--left-right", "--count", f"HEAD...{upstream}")
            ahead, behind = (int(value) for value in counts.split())
        return {
            "root": str(self.root),
            "remote": remote,
            "url": url,
            "branch": branch,
            "upstream": upstream or None,
            "ahead": ahead,
            "behind": behind,
            "dirty": dirty,
            "clean": not dirty,
        }

    def fetch(self, remote: str = "origin") -> dict[str, Any]:
        self._name(remote, "remote")
        self._git("fetch", "--prune", remote)
        return {**self.status(remote), "fetched": True}

    def publish(
        self,
        *,
        message: str,
        remote: str = "origin",
        create_pr: bool = False,
        base: str | None = None,
    ) -> dict[str, Any]:
        self._name(remote, "remote")
        if not message.strip():
            raise EpiqError("invalid_commit_message", "Commit message cannot be empty")
        health = diagnose(self.root)
        if not health["healthy"]:
            raise EpiqError("invalid_ledger", "Refusing to publish an unhealthy ledger")
        owned = [
            name
            for name in ("epiq.toml", ".gitignore", "ledger", "artifacts")
            if (self.root / name).exists()
        ]
        self._git("add", "-A", "--", *owned)
        staged = self._git_optional("diff", "--cached", "--name-only").splitlines()
        unexpected = [path for path in staged if not self._owned_path(path)]
        if unexpected:
            raise EpiqError(
                "unrelated_staged_changes",
                f"Refusing to include non-Epiq staged paths: {unexpected}",
                "Commit or unstage those paths before publishing the Epiq ledger.",
            )
        committed = bool(staged)
        if committed:
            self._git("commit", "-m", message)
        branch = self._git("branch", "--show-current")
        if not branch:
            raise EpiqError("detached_head", "Create or check out a branch before publishing")
        self._git("push", "-u", remote, f"HEAD:{branch}")
        pr_url = None
        if create_pr:
            arguments = ["gh", "pr", "create", "--fill", "--head", branch]
            if base:
                self._name(base, "base branch")
                arguments.extend(("--base", base))
            pr_url = self._run_at(self.root, *arguments)
        return {
            "root": str(self.root),
            "remote": remote,
            "branch": branch,
            "committed": committed,
            "staged_paths": staged,
            "pushed": True,
            "pull_request": pr_url,
            "transaction_set_digest": health["ledger"]["transaction_set_digest"],
        }

    def _git(self, *arguments: str) -> str:
        return self._run_at(self.root, "git", *arguments)

    def _git_optional(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=self.root, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def _owned_path(path: str) -> bool:
        return path in {"epiq.toml", ".gitignore"} or path.startswith(("ledger/", "artifacts/"))

    @staticmethod
    def _name(value: str, label: str) -> None:
        if not value or value.startswith("-") or not _SAFE_NAME.fullmatch(value):
            raise EpiqError("invalid_git_name", f"Invalid Git {label}: {value}")

    @staticmethod
    def _run_at(root: Path, *arguments: str) -> str:
        try:
            result = subprocess.run(
                list(arguments), cwd=root, check=True, capture_output=True, text=True
            )
        except FileNotFoundError as error:
            raise EpiqError(
                "git_tool_missing", f"Command is not installed: {arguments[0]}"
            ) from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() or error.stdout.strip() or "Git command failed"
            raise EpiqError("git_command_failed", detail) from error
        return result.stdout.strip()
