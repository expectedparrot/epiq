"""Canonical transport bindings and safety policy for Epiq operations."""

from __future__ import annotations

from typing import Any


def _operation(
    command: str,
    method: str | None,
    path: str | None,
    safety: str,
    *,
    equivalence: str = "direct",
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "operation": command.replace("-", "."),
        "cli_command": command,
        "api": {"method": method, "path": path} if method and path else None,
        "equivalence": equivalence,
        "safety": safety,
        "agent_available": safety in {"read", "additive", "corrective", "review_required"}
        and method is not None,
        "reason": reason,
    }


_BINDINGS = [
    ("init", "POST", "/api/projects", "additive"),
    ("use", "POST", "/api/projects/open", "administrative"),
    ("db", "GET", "/api/health", "read"),
    ("apply", "POST", "/api/apply", "additive"),
    ("seed", "POST", "/api/apply", "additive", "shared"),
    ("doctor", "GET", "/api/doctor", "read"),
    ("backup", "GET", "/api/export/project.sqlite", "read"),
    ("export-bundle", "GET", "/api/export/project.epiq", "read"),
    ("capabilities", "GET", "/api/capabilities", "read"),
    ("schema", "GET", "/api/schema", "read"),
    ("context", "GET", "/api/context", "read"),
    ("gaps", "GET", "/api/gaps/{entity_kind}", "read"),
    ("stale", "GET", "/api/stale/{entity_kind}", "read"),
    ("contradictions", "GET", "/api/contradictions/{entity_kind}", "read"),
    ("refresh-plan", "GET", "/api/refresh-plan/{entity_kind}", "read"),
    ("search", "GET", "/api/search", "read"),
    ("entity", "POST", "/api/entities", "additive"),
    ("entity-alias", "POST", "/api/entities/{entity_id}/aliases", "additive"),
    ("merge-entities", "POST", "/api/entities/{entity_id}/merge", "corrective"),
    ("retire-entity", "POST", "/api/entities/{entity_id}/retire", "corrective"),
    ("restore-entity", "POST", "/api/entities/{entity_id}/restore", "corrective"),
    ("question", "POST", "/api/questions", "additive"),
    ("retire-question", "POST", "/api/questions/{question_id}/retire", "corrective"),
    ("restore-question", "POST", "/api/questions/{question_id}/restore", "corrective"),
    ("evolve-question", "POST", "/api/questions/{question_id}/evolve", "corrective"),
    ("question-lineage", "GET", "/api/questions/{question_id}/lineage", "read"),
    ("evidence", "POST", "/api/evidence", "additive"),
    ("assess-evidence", "POST", "/api/evidence/{evidence_id}/assess", "corrective"),
    ("evidence-assessments", "GET", "/api/evidence/{evidence_id}/assessments", "read"),
    ("assert", "POST", "/api/claims", "additive"),
    ("bulk-assert", "POST", "/api/claims/bulk", "additive"),
    ("batch-write", "POST", "/api/batch", "additive"),
    ("record", "POST", "/api/batch", "additive", "composed"),
    ("propose-claim", "POST", "/api/claim-proposals", "review_required"),
    ("claim-proposals", "GET", "/api/claim-proposals", "read"),
    ("review-claims", "POST", "/api/claim-proposals/review", "review_required"),
    ("retract", "POST", "/api/claims/{claim_id}/retract", "corrective"),
    ("end-validity", "POST", "/api/claims/{claim_id}/validity-end", "corrective"),
    ("supersede", "POST", "/api/claims/{claim_id}/supersede", "corrective"),
    ("challenge-question", "POST", "/api/questions/{question_id}/challenges", "corrective"),
    ("question-challenges", "GET", "/api/question-challenges", "read"),
    (
        "resolve-question-challenge",
        "POST",
        "/api/question-challenges/{challenge_id}/resolve",
        "review_required",
    ),
    ("season-record", "GET", "/api/reports/season-record/{season}", "read"),
    ("history", "GET", "/api/history", "read"),
    ("check", "POST", "/api/epiql/check", "read"),
    ("matrix", "GET", "/api/matrix/{entity_kind}", "read"),
    ("query", "POST", "/api/query/{entity_kind}", "read"),
    ("aggregate", "POST", "/api/aggregate/{entity_kind}", "read"),
    ("dossier", "GET", "/api/reports/dossier/{entity}", "read"),
    ("related", "GET", "/api/related/{entity}", "read"),
    ("timeline", "GET", "/api/reports/timeline/{entity_kind}/{question}", "read"),
    ("delta", "POST", "/api/reports/delta", "additive"),
    ("export-xlsx", "GET", "/api/export/{entity_kind}.xlsx", "read"),
    ("export-edsl", "GET", "/api/export/{entity_kind}.{object_type}.ep", "read"),
    ("export", "GET", "/api/export/{format}", "read", "family"),
    ("export-html", "GET", "/api/export/{entity_kind}.html", "read"),
    ("not-found", "POST", "/api/research/not-found", "additive"),
    ("derive-distribution", "POST", "/api/derive-distribution", "additive"),
    ("derive", "POST", "/api/derive", "additive"),
    ("materialize", "POST", "/api/materialize", "additive"),
    ("propagate", "POST", "/api/propagate", "additive"),
    ("stale-derivations", "GET", "/api/stale-derivations", "read"),
]

_LOCAL_ONLY = [
    ("migration-plan", "administrative", "Database-file migration inspection is server-local."),
    ("migrate", "administrative", "Hosted databases migrate under server lifecycle control."),
    ("import-bundle", "administrative", "Bundle upload needs a dedicated managed-project import."),
    ("demo", "development", "Fixture loading is a developer convenience, not an agent operation."),
    (
        "import-cham",
        "legacy",
        "Legacy corpus migration is intentionally kept out of the research API.",
    ),
]


def operation_catalog() -> list[dict[str, Any]]:
    """Return every CLI command with its HTTP binding or explicit exclusion."""
    items = []
    for binding in _BINDINGS:
        command, method, path, safety = binding[:4]
        items.append(
            _operation(
                command,
                method,
                path,
                safety,
                equivalence=binding[4] if len(binding) > 4 else "direct",
            )
        )
    items.extend(
        _operation(command, None, None, safety, equivalence="excluded", reason=reason)
        for command, safety, reason in _LOCAL_ONLY
    )
    return sorted(items, key=lambda item: str(item["cli_command"]))


def agent_operation_catalog() -> list[dict[str, Any]]:
    """Compact catalog of operations exposed to autonomous workspace agents."""
    return [
        {
            "operation": item["operation"],
            "method": item["api"]["method"],
            "path": item["api"]["path"],
            "safety": item["safety"],
        }
        for item in operation_catalog()
        if item["agent_available"]
    ]
