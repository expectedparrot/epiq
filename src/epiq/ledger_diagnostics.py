"""Offline integrity and merge diagnostics for Git-native Epiq projects."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .ledger import LedgerRepository
from .ledger_projection import LedgerProjector


def diagnose(root: str | Path, *, rebuild: bool = False) -> dict[str, Any]:
    """Validate the ledger and projection without network or Git dependencies."""
    repository = LedgerRepository(Path(root))
    ledger = repository.validate()
    projector = LedgerProjector(repository)
    build = projector.build(force=rebuild)
    projection = projector.verify()
    projection["semantic_fingerprint"] = projector.semantic_fingerprint()
    with sqlite3.connect(projector.path) as connection:
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        conflicts = [
            {
                "conflict_id": str(row[0]),
                "conflict_type": str(row[1]),
                "object_ids": json.loads(str(row[2])),
                "detail": json.loads(str(row[3])),
            }
            for row in connection.execute(
                """SELECT conflict_id,conflict_type,object_ids_json,detail_json
                   FROM projected_conflicts ORDER BY conflict_type,conflict_id"""
            )
        ]
        retained_reports = [
            (str(row[0]), json.loads(str(row[1])))
            for row in connection.execute(
                "SELECT content_digest,parameters_json FROM projected_reports"
            )
        ]
    artifact_store = ArtifactStore(repository.root)
    artifacts = artifact_store.verify_all()
    for digest, parameters in retained_reports:
        if parameters.get("artifact_retention") in {"git", "git-lfs"}:
            artifact_store.verify(digest)
    ignored = _projection_is_ignored(repository.root)
    checks = [
        {"name": "ledger_integrity", "status": "pass"},
        {"name": "projection_verification", "status": "pass"},
        {
            "name": "projection_foreign_keys",
            "status": "pass" if not foreign_keys else "fail",
            "violations": [list(row) for row in foreign_keys],
        },
        {
            "name": "projection_ignored",
            "status": "pass" if ignored else "warn",
            "message": ".epiq/ is ignored" if ignored else ".epiq/ is not listed in .gitignore",
        },
        {
            "name": "domain_conflicts",
            "status": "pass" if not conflicts else "warn",
            "count": len(conflicts),
        },
        {
            "name": "retained_artifacts",
            "status": "pass",
            "count": len(artifacts),
        },
    ]
    return {
        "root": str(repository.root),
        "healthy": not foreign_keys,
        "clean": not foreign_keys and not conflicts,
        "ledger": ledger,
        "projection": projection,
        "build_mode": build["mode"],
        "checks": checks,
        "conflicts": conflicts,
    }


def _projection_is_ignored(root: Path) -> bool:
    path = root / ".gitignore"
    if not path.exists():
        return False
    entries = {
        line.strip().removeprefix("/").removesuffix("/")
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return ".epiq" in entries
