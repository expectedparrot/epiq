from pathlib import Path

import pytest

import epiq
from epiq.errors import EpiqError


def clock() -> str:
    return "2026-11-21T12:00:00Z"


def test_open_create_transact_and_read(tmp_path: Path) -> None:
    database = epiq.open_ledger(
        tmp_path / "project",
        create=True,
        name="Project",
        project_id="prj_api",
        replica_id="rep_api",
        clock=clock,
    )
    result = database.transact(
        [
            {"type": "entity_kind.define", "payload": {"kind": "Item"}},
            {
                "type": "entity.create",
                "payload": {"entity_id": "ent_one", "kind": "Item", "name": "One"},
            },
        ],
        idempotency_key="create-one",
    )

    assert result["created"] is True
    assert database.entities() == [
        {
            "entity_id": "ent_one",
            "kind": "Item",
            "name": "One",
            "attributes": {},
            "role": "entity",
            "identity": None,
            "visible": True,
            "visibility_conflicted": False,
        }
    ]
    assert database.status()["state"] == "ready"

    reopened = epiq.open_ledger(tmp_path / "project", clock=clock)
    assert reopened.entities() == database.entities()


def test_transact_idempotency_and_validation(tmp_path: Path) -> None:
    database = epiq.open_ledger(
        tmp_path / "project",
        create=True,
        project_id="prj_api",
        replica_id="rep_api",
        clock=clock,
    )
    operation = {"type": "entity_kind.define", "payload": {"kind": "Item"}}
    first = database.transact([operation], idempotency_key="same")
    second = database.transact([operation], idempotency_key="same")
    assert first["transaction_id"] == second["transaction_id"]
    assert second["created"] is False

    with pytest.raises(EpiqError, match="another request"):
        database.transact(
            [{"type": "entity_kind.define", "payload": {"kind": "Other"}}],
            idempotency_key="same",
        )


def test_existing_non_project_directory_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "not-a-project"
    path.mkdir()
    with pytest.raises(EpiqError):
        epiq.open_ledger(path)


def test_estimated_claim_metadata_round_trips(tmp_path: Path) -> None:
    database = epiq.open_ledger(tmp_path / "project", create=True, name="Estimates")
    database.define_kind("Food")
    entity = database.create_entity("Food", "Cheese")["entity_id"]
    question = database.define_question("kcal_per_100g", "Food", "Float")["question_id"]
    result = database.record(
        entity,
        question,
        350.0,
        valid_from="2026-11-21",
        source_title="Food composition reference",
        source_type="report",
        url="https://example.test/food",
        retrieved_at="2026-11-21",
        excerpt="Comparable cheeses range from 250 to 450 kcal per 100 g.",
        epistemic_status="estimated",
        estimate={
            "lower": 250,
            "upper": 450,
            "method": "Reference-class midpoint",
            "assumptions": ["Unspecified ordinary full-fat cheese"],
            "proxy": "Comparable cheeses",
        },
    )

    claim = next(item for item in database.claims() if item["claim_id"] == result["claim_id"])
    assert claim["epistemic_status"] == "estimated"
    assert claim["estimate"]["lower"] == 250
    assert claim["estimate"]["upper"] == 450


def test_high_level_research_workflow_is_ledger_authoritative(tmp_path: Path) -> None:
    root = tmp_path / "project"
    database = epiq.open_ledger(
        root,
        create=True,
        project_id="prj_workflow",
        replica_id="rep_workflow",
        clock=clock,
    )
    database.define_kind("Company", idempotency_key="kind-company")
    entity = database.create_entity(
        "Company", "Acme", idempotency_key="entity-acme"
    )
    question = database.define_question(
        "employees",
        "Company",
        "Int",
        idempotency_key="question-employees",
    )
    recorded = database.record(
        entity["entity_id"],
        question["question_id"],
        12,
        valid_from="2026-11-21",
        source_title="Acme profile",
        source_type="web",
        url="https://example.test/acme",
        retrieved_at="2026-11-21",
        excerpt="Acme has 12 employees.",
        idempotency_key="record-employees",
    )
    retried = database.record(
        entity["entity_id"],
        question["question_id"],
        12,
        valid_from="2026-11-21",
        source_title="Acme profile",
        source_type="web",
        url="https://example.test/acme",
        retrieved_at="2026-11-21",
        excerpt="Acme has 12 employees.",
        idempotency_key="record-employees",
    )

    assert retried["created"] is False
    assert retried["transaction_id"] == recorded["transaction_id"]
    assert retried["claim_id"] == recorded["claim_id"]
    assert database.matrix("Company")["cells"][entity["entity_id"]][
        question["question_id"]
    ][0]["value"] == 12

    (root / ".epiq" / "projection.sqlite").unlink()
    reopened = epiq.open_ledger(root, clock=clock)
    assert reopened.claims()[0]["claim_id"] == recorded["claim_id"]


def test_quantity_claims_are_normalized_to_schema_unit(tmp_path: Path) -> None:
    database = epiq.open_ledger(tmp_path / "project", create=True, clock=clock)
    database.define_kind("FundingRound")
    entity = database.create_entity("FundingRound", "Seed")["entity_id"]
    question = database.define_question(
        "amount",
        "FundingRound",
        "Quantity[USD]",
    )["question_id"]
    evidence = database.capture_evidence(
        source_title="Funding announcement",
        source_type="web",
        url="https://example.test/funding",
        retrieved_at="2026-11-21",
        excerpt="The company raised $5.3 million.",
    )["evidence_id"]

    database.assert_claim(
        entity,
        question,
        {"amount": 5_300_000, "currency": "USD"},
        valid_from="2026-11-21",
        evidence_ids=[evidence],
    )

    assert database.claims()[0]["value"] == 5_300_000
    with pytest.raises(EpiqError, match="received unit 'EUR'"):
        database.assert_claim(
            entity,
            question,
            {"amount": 5_000_000, "currency": "EUR"},
            valid_from="2026-11-21",
            evidence_ids=[evidence],
        )
