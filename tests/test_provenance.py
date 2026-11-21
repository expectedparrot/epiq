from copy import deepcopy
from pathlib import Path

import pytest

import epiq
from epiq.errors import EpiqError
from epiq.provenance import digest
from epiq.store import Store
from epiq.xlsx import projection_rows


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "capture.sqlite")
    store.initialize("Captures")
    store.add_entity("Town", "A", {}, "setup")
    store.add_question("population", "Town", "Int", {}, "setup")
    return store


def operations(method="fetched", retain=False):
    capture = {
        "capture_method": method,
        "provider": "fixture",
        "captured_at": "2026-09-09T12:00:00Z",
    }
    if method != "discovery":
        capture.update(content="Town A census. Population: 7. End.", retain_content=retain)
    return [
        {
            "op": "evidence.add",
            "ref": "s",
            "url": "https://example.test",
            "title": "Census",
            "retrieved_at": "2026-09-09",
            "excerpt": "Population: 7.",
            "provenance": capture,
        },
        {
            "op": "claim.assert",
            "subject": "A",
            "question": "population",
            "value": 7,
            "valid_from": "2026-09-09",
            "evidence_refs": ["s"],
            "supports": [{"evidence_ref": "s", "quote": "Population: 7."}],
        },
    ]


def test_capture_support_review_and_retries_are_independent(store):
    first = store.write_batch(operations(retain=True), "agent:research")
    assert store.write_batch(operations(retain=True), "agent:research") == first
    cell = store.matrix("Town")["rows"][0]["cells"]["population"]
    assert cell["assurance"] == {"captured": True, "supported": True, "reviewed": False}
    lineage = cell["lineage"][0]
    assert lineage["provenance"]["content_hash"] == digest("Town A census. Population: 7. End.")
    assert lineage["support"][0]["quote"] == "Population: 7."
    assert lineage["review"]["status"] == "unreviewed"
    report = store.doctor()
    assert report["ok"] and report["scope"] == "structural_integrity"
    assert {f["code"] for f in report["provenance"]["findings"]} == {"unreviewed_claim"}
    cutoff = store.history()[-1]["recorded_at"]
    store.review_claim(first[1]["claim_id"], "accepted", "Checked the source and value", "human:a")
    assert store.doctor()["provenance"]["ok"]
    assert (
        store.matrix("Town", known_at=cutoff)["rows"][0]["cells"]["population"]["assurance"][
            "reviewed"
        ]
        is False
    )
    current = store.matrix("Town")["rows"][0]["cells"]["population"]
    assert current["assurance"]["reviewed"] is True
    before = store.history()
    store.review_claim(first[1]["claim_id"], "accepted", "Checked the source and value", "human:a")
    assert store.history() == before
    with store.connect() as connection:
        assert (
            connection.execute("SELECT content FROM source_artifacts")
            .fetchone()[0]
            .startswith("Town A")
        )


def test_discovery_can_be_saved_but_is_never_verified(store):
    store.write_batch(operations("discovery"), "agent:firecrawl")
    lineage = store.matrix("Town")["rows"][0]["cells"]["population"]["lineage"][0]
    assert lineage["provenance"]["capture_method"] == "discovery"
    assert lineage["provenance"]["verification_status"] == "unverified"
    report = store.doctor()
    assert report["ok"]
    assert {f["code"] for f in report["provenance"]["findings"]} >= {
        "discovery_only_evidence",
        "unreviewed_claim",
    }


@pytest.mark.parametrize("change", ["quote", "hash", "excerpt", "missing_content"])
def test_invalid_capture_and_support_roll_back_entire_batch(store, change):
    ops = deepcopy(operations())
    if change == "quote":
        ops[1]["supports"][0]["quote"] = "Unrelated value not in the excerpt"
    elif change == "hash":
        ops[0]["provenance"]["content_hash"] = "wrong"
    elif change == "excerpt":
        ops[0]["excerpt"] = "Unsupported citation"
    else:
        del ops[0]["provenance"]["content"]
    before = store.history()
    with pytest.raises(EpiqError):
        store.write_batch(ops, "agent:research")
    assert store.history() == before
    assert store.list_claims() == []


def test_legacy_evidence_and_unrelated_excerpt_are_explicitly_unreviewed(store):
    ops = operations()
    del ops[0]["provenance"]
    ops[0]["excerpt"] = "The town has a swimming pool."
    del ops[1]["supports"]
    store.write_batch(ops, "agent:research")
    cell = store.matrix("Town")["rows"][0]["cells"]["population"]
    assert cell["state"] == "Answered"
    assert cell["assurance"] == {"captured": False, "supported": False, "reviewed": False}
    assert {f["code"] for f in store.provenance_diagnostics()["findings"]} == {
        "unverified_capture",
        "missing_claim_support",
        "unreviewed_claim",
    }


def test_exports_include_procedural_provenance(store):
    store.write_batch(operations(), "agent:research")
    rows = projection_rows(store.matrix("Town"))
    evidence = rows["Evidence"]
    assert "Capture method" in evidence[0]
    assert "fetched" in evidence[1]
    assert "unreviewed" in evidence[1]
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 0


def test_staged_capture_does_not_retain_artifacts_until_approval(store):
    db = epiq.open(store.path)
    proposal = db.propose({"operations": operations(retain=True)})
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0] == 0
    db.review_proposal(proposal, "approved", "Checked content and calculation")
    assert store.matrix("Town")["rows"][0]["cells"]["population"]["assurance"] == {
        "captured": True,
        "supported": True,
        "reviewed": True,
    }


@pytest.mark.parametrize("supports", [None, "quote", [None], [{"evidence_ref": []}]])
def test_malformed_support_returns_domain_error_and_rolls_back(store, supports):
    ops = operations()
    ops[1]["supports"] = supports
    before = store.history()
    with pytest.raises(EpiqError):
        store.write_batch(ops, "agent:test")
    assert store.history() == before


def test_staging_requires_explicit_retention_of_full_capture(store):
    before = store.history()
    with pytest.raises(EpiqError, match="retain_content=True"):
        epiq.open(store.path).propose({"operations": operations(retain=False)})
    assert store.history() == before
    ops = operations()
    ops[0]["provenance"]["retain_content"] = "false"
    with pytest.raises(EpiqError, match="boolean"):
        store.write_batch(ops, "agent:test")
    assert store.history() == before
