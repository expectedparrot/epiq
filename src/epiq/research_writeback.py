"""Prepare one scalar research finding for atomic storage with its job checkpoint."""

from datetime import UTC, datetime
from typing import Any

from .errors import EpiqError
from .store import Store, _normalize_excerpt, canonicalize_url


def captured_operations(finding: dict[str, Any]):
    captured_sources = finding.get("_captured_sources", [])
    operations = []
    refs = []
    supports = []
    for index, source in enumerate(captured_sources):
        reference = f"finding-{index}"
        quote = _normalize_excerpt(source["excerpt"])
        refs.append(reference)
        operations.append(
            {
                "op": "evidence.add",
                "ref": reference,
                "url": source["canonical_url"],
                "title": source["title"],
                "retrieved_at": source["retrieved_at"],
                "excerpt": quote,
                "published_at": source["published_at"],
                "source_type": source["source_type"],
                "provenance": {
                    "capture_method": source["capture_method"],
                    "content": source["markdown"],
                    "captured_at": source["retrieved_at"],
                    "content_hash": source["content_hash"],
                    "provider": source["provider"],
                    "retain_content": finding.get("_retain_content", False),
                },
            }
        )
        supports.append({"evidence_ref": reference, "quote": quote})
    return operations, refs, supports


def prepare_finding(
    finding: dict[str, Any],
    target: dict[str, Any],
    question: dict[str, Any],
    mode: str,
) -> tuple[list[dict[str, Any]], dict[str, str] | None, dict[str, int], str]:
    counts = {"written": 0, "no_result": 0, "rejected": 0}
    name = target["name"]
    if finding["status"] == "not_found":
        counts["no_result"] = 1
        if mode == "add_evidence":
            message = f"No additional independent source found for {name}"
            if finding.get("notes"):
                message += f": {str(finding['notes']).strip()[:500]}"
            return [], None, counts, message
        counts["written"] = 1
        return (
            [],
            {
                "subject": target["entity_id"],
                "question": question["question_id"],
                "query": f"Agent research for {question['name']}",
                "notes": str(finding.get("notes") or "No sufficient evidence found."),
            },
            counts,
            f"No sufficient evidence found for {name}",
        )
    if finding["status"] != "answered":
        raise EpiqError("invalid_research_result", f"Unknown finding status: {finding['status']}")
    value = finding["value"]
    many = question["definition"].get("cardinality", "one") == "many"
    values = value if many and isinstance(value, list) else [value]
    if not values:
        counts["no_result"] = 1
        return [], None, counts, f"No supported values found for {name}"
    for candidate in values:
        Store._check_value_type(question["value_type"], candidate)
    source_type = str(finding.get("source_type") or "web")
    source_url = str(finding.get("source_url") or f"urn:epiq:{source_type}")
    if mode == "add_evidence" and canonicalize_url(source_url) in {
        canonicalize_url(str(url)) for url in target["existing_source_urls"]
    }:
        counts["rejected"] = 1
        return [], None, counts, f"Rejected duplicate source for {name}: {source_url}"
    today = datetime.now(UTC).date().isoformat()
    same_value = mode == "add_evidence" and value == target["existing_value"]
    valid_from = str(
        (
            target.get("existing_valid_from")
            if same_value
            else finding.get("observed_as_of") or finding.get("source_published_at")
        )
        or today
    )
    operations = [
        {
            "op": "evidence.add",
            "ref": "finding",
            "url": source_url,
            "title": str(finding["source_title"]),
            "retrieved_at": today,
            "excerpt": str(finding["excerpt"]),
            "published_at": finding.get("source_published_at"),
            "source_type": source_type,
        }
    ]
    captured_sources = finding.get("_captured_sources", [])
    refs = ["finding"]
    supports = []
    if captured_sources:
        operations, refs, supports = captured_operations(finding)
    operations.extend(
        {
            "op": "claim.assert",
            "subject": target["entity_id"],
            "question": question["question_id"],
            "value": candidate,
            "valid_from": valid_from,
            "evidence_ids": target["existing_evidence_ids"] if same_value else [],
            "evidence_refs": refs,
            "supports": supports,
            "confidence": str(finding["confidence"]),
            "temporal_basis": "observed"
            if finding.get("observed_as_of")
            else "source"
            if finding.get("source_published_at")
            else "unknown",
        }
        for candidate in values
    )
    counts["written"] = 1
    message = f"Finished research for {name}"
    if mode == "add_evidence" and not same_value:
        message = (
            f"Found a conflicting value for {name}: "
            f"existing {target['existing_value']!r}, new source {value!r}"
        )
    return operations, None, counts, message
