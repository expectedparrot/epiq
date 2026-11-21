from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest

import epiq
from epiq.errors import EpiqError
from epiq.store import Store


def document():
    return {
        "entities": [{"kind": "X", "name": "A"}],
        "questions": [{"name": "q", "subject_kind": "X", "value_type": "Int"}],
        "operations": [
            {
                "op": "evidence.add",
                "ref": "source",
                "url": "https://example.test",
                "title": "Test",
                "retrieved_at": "2018-01-13",
                "excerpt": "The answer is 7.",
            },
            {
                "op": "claim.assert",
                "subject": "A",
                "question": "q",
                "value": 7,
                "valid_from": "2018-01-13",
                "evidence_refs": ["source"],
            },
        ],
    }


@pytest.fixture
def database(tmp_path: Path):
    return epiq.open_legacy_database(tmp_path / "proposals.sqlite", create=True)


def test_staged_document_concurrent_retries_and_atomic_acceptance(database):
    def submit(_):
        return epiq.open_legacy_database(database.path).propose(
            document(), actor="agent:a", idempotency_key="run-1"
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(submit, range(3)))
    assert len(set(ids)) == 1
    assert database.matrix("X")["rows"] == []
    assert database.claims() == []
    assert len(database.proposals()) == 1
    assert database.proposals()[0]["document"] == document()
    assert {e["event_type"] for e in Store(database.path).history()} == {"document.propose"}
    approved = database.review_proposal(ids[0], "approved", "Checked source")
    assert database.matrix("X")["rows"][0]["cells"]["q"]["value"] == 7
    assert len(database.claims()) == 1
    before = Store(database.path).history()
    assert submit(0) == ids[0]
    assert database.review_proposal(ids[0], "approved", "Retry") == approved
    assert Store(database.path).history() == before
    assert database.proposals(status=None)[0]["document"] == document()


def test_staged_late_invalid_candidate_leaves_no_scaffolds_or_receipt(database):
    invalid = document()
    invalid["operations"].append(dict(invalid["operations"][-1], value="invalid"))
    with pytest.raises(EpiqError):
        database.propose(invalid, idempotency_key="retry-after-correction")
    assert Store(database.path).history() == []
    assert database.matrix("X")["rows"] == []
    database.propose(document(), idempotency_key="retry-after-correction")
    assert len(database.proposals()) == 1


def test_staged_approval_revalidates_and_rolls_back(database):
    store = Store(database.path)
    store.add_question("q", "X", "Int", {}, "setup")
    proposal = database.propose(document())
    store.set_question_visibility("q", False, "Retired", "reviewer")
    before = store.history()
    with pytest.raises(EpiqError, match="retired"):
        database.review_proposal(proposal, "approved", "Attempt acceptance")
    assert store.history() == before
    assert database.matrix("X")["rows"] == []
    assert database.proposals()[0]["status"] == "pending"


def test_staged_retractions_and_not_found_are_invisible_until_acceptance(database):
    store = Store(database.path)
    store.apply_document(document(), "setup")
    claim = database.claims()[0]["claim_id"]
    staged = {
        "operations": [
            {"op": "claim.retract", "claim_id": claim, "reason": "Unsupported"},
            {
                "op": "research.not_found",
                "subject": "A",
                "question": "q",
                "query": "official archive",
                "notes": "No sufficient evidence",
            },
        ]
    }
    proposal = database.propose(staged)
    assert database.matrix("X")["rows"][0]["cells"]["q"]["state"] == "Answered"
    database.review_proposal(proposal, "approved", "Reviewed research")
    assert database.matrix("X")["rows"][0]["cells"]["q"]["state"] == "NotFound"
    assert database.claims() == []
    assert store.doctor()["ok"]


def test_proposal_keys_are_actor_scoped_and_decisions_are_final(database):
    first = database.propose(document(), actor="agent:a", idempotency_key="same")
    second = database.propose(document(), actor="agent:b", idempotency_key="same")
    assert first != second
    changed = document()
    changed["operations"][-1]["value"] = 8
    with pytest.raises(EpiqError) as error:
        database.propose(changed, actor="agent:a", idempotency_key="same")
    assert error.value.code == "idempotency_conflict"
    database.review_proposal(first, "rejected", "Source insufficient")
    assert database.propose(document(), actor="agent:a", idempotency_key="same") == first
    with pytest.raises(EpiqError, match="cannot change decision"):
        database.review_proposal(first, "approved", "Changed my mind")
    assert database.propose(document(), actor="agent:a", idempotency_key="new") != first


def test_individual_proposals_and_research_retries_are_durable(database):
    store = Store(database.path)
    store.apply_document(document(), "setup")
    evidence = database.claims()[0]["evidence_id"]

    def submit(_):
        return Store(database.path).propose_claim("A", "q", 8, "2018-01-13", [evidence], "agent:a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        proposals = list(pool.map(submit, range(2)))
    assert len(set(proposals)) == 1
    result = store.review_claim_proposals(proposals, "rejected", "Unsupported", "human:a")
    assert store.review_claim_proposals(proposals, "rejected", "Retry", "human:a") == result
    assert submit(0) == proposals[0]

    def search(_):
        return Store(database.path).record_not_found("A", "q", "archive", "nothing", "agent:a")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(search, range(2)))
    assert len(set(tasks)) == 1
    assert store.record_not_found("A", "q", "archive", "nothing", "agent:a", "new-run") != tasks[0]


def test_historical_document_import_and_retry_after_later_phase(database):
    phase1 = database.import_document(
        document(), recorded_at="2018-01-13T08:07:00-10:00", idempotency_key="phase-1"
    )
    store = Store(database.path)
    assert {event["recorded_at"] for event in store.history()} == {"2018-01-13T18:07:00.000000Z"}
    earlier = database.matrix("X", known_at="2018-01-13T18:07:00Z")
    cell = earlier["rows"][0]["cells"]["q"]
    assert cell["value"] == 7
    assert len(cell["lineage"]) == 1
    claim = database.claims()[0]["claim_id"]
    phase2 = {
        "operations": [
            {"op": "claim.retract", "claim_id": claim, "reason": "Incorrect"},
            {"op": "research.not_found", "subject": "A", "question": "q", "query": "archive"},
        ]
    }
    database.import_document(phase2, recorded_at="2018-01-14T00:00:00Z")
    assert database.matrix("X", known_at="2018-01-13T18:07:00Z") == earlier
    assert (
        database.matrix("X", known_at="2018-01-14T00:00:00Z")["rows"][0]["cells"]["q"]["state"]
        == "NotFound"
    )
    before = store.history()
    assert (
        database.import_document(
            document(), recorded_at="2018-01-13T18:07:00Z", idempotency_key="phase-1"
        )
        == phase1
    )
    assert store.history() == before
    assert store.doctor()["ok"]
    with pytest.raises(EpiqError) as error:
        database.import_document({}, recorded_at="2018-01-12")
    assert error.value.code == "invalid_chronology"
    assert store.history() == before


def test_historical_import_failure_and_invalid_clock_roll_back(database):
    invalid = deepcopy(document())
    invalid["operations"][-1]["value"] = "invalid"
    with pytest.raises(EpiqError):
        database.import_document(invalid, recorded_at="2018-01-13", idempotency_key="first")
    assert Store(database.path).history() == []
    database.import_document(document(), recorded_at="2018-01-13", idempotency_key="first")
    clock = epiq.open_legacy_database(database.path, clock=lambda: "2017-01-01")
    before = Store(database.path).history()
    with pytest.raises(EpiqError) as error:
        clock.upsert_entity("X", "Backwards")
    assert error.value.code == "invalid_chronology"
    assert Store(database.path).history() == before


def test_public_clock_controls_all_fixture_creation(tmp_path):
    db = epiq.open_legacy_database(
        tmp_path / "fixture.sqlite", create=True, clock=lambda: "2018-01-13T23:00:00Z"
    )
    db.upsert_entity("X", "A")
    db.define_question("q", "X", "Int")
    db.batch_write(document()["operations"])
    assert db.matrix("X", known_at="2018-01-14")["rows"][0]["cells"]["q"]["value"] == 7
    assert {e["recorded_at"] for e in Store(db.path).history()} == {"2018-01-13T23:00:00.000000Z"}


def test_import_evidence_links_preserve_earlier_snapshot_and_bundle(database, tmp_path):
    database.import_document(document(), recorded_at="2018-01-13")
    before = database.matrix("X", known_at="2018-01-13")
    extra = document()["operations"]
    extra[0]["url"] = "https://example.test/extra"
    extra[0]["excerpt"] = "Further confirmation of 7."
    database.import_document({"operations": extra}, recorded_at="2018-01-14")
    assert database.matrix("X", known_at="2018-01-13") == before
    assert len(database.matrix("X", known_at="2018-01-14")["rows"][0]["cells"]["q"]["lineage"]) == 2
    store = Store(database.path)
    archive = store.export_bundle(tmp_path / "historical.epiq")
    restored = Store.import_bundle(archive, tmp_path / "restored.sqlite")
    assert restored.history() == store.history()
    assert restored.matrix("X", known_at="2018-01-13") == before
    assert restored.doctor()["ok"]


def test_migration_from_v13_preserves_facts_and_adds_retry_tables(database):
    store = Store(database.path)
    store.apply_document(document(), "setup")
    history = store.history()
    with store.transaction() as connection:
        connection.execute("DROP TABLE write_receipts")
        connection.execute("DROP TABLE document_proposals")
        connection.execute("UPDATE meta SET value='13' WHERE key='schema_version'")
    store = Store(database.path)
    proposal = store.propose_document(document(), "test")
    assert store.history()[:-1] == history
    assert store.propose_document(document(), "test") == proposal
    assert store.overview()["project"]["schema_version"] == "15"


def test_invalid_bundle_operations_roll_back_and_preserve_pending_review(database):
    for operation in (None, 42, [], "claim.assert"):
        with pytest.raises(EpiqError) as error:
            database.propose({"operations": [operation]})
        assert error.value.code == "invalid_batch_item"
    assert Store(database.path).history() == []
    Store(database.path).apply_document(document(), "setup")
    claim = database.claims()[0]["claim_id"]
    proposal = database.propose(
        {
            "operations": [
                {"op": "claim.retract", "claim_id": claim, "reason": "Reconsider"},
                {"op": "research.not_found", "subject": "A", "question": "q", "query": "archive"},
            ]
        }
    )
    # A concurrent review already closed the target; acceptance must revalidate it.
    Store(database.path).close_claim(claim, "retracted", "Another review", "human:b")
    before = Store(database.path).history()
    with pytest.raises(EpiqError):
        database.review_proposal(proposal, "approved", "Review")
    assert Store(database.path).history() == before
    assert database.proposals()[0]["status"] == "pending"
