import json

import pytest

from epiq.agent_interface import capabilities_data, docs_show_data, guide_data, schema_data
from epiq.cli import capabilities, main
from epiq.errors import EpiqError
from epiq.schema_guidance import schema_plan


def test_builtin_guide_is_ledger_native_and_topic_addressable(capsys) -> None:
    main(["guide", "git"])
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["status"] == "ok"
    assert envelope["command"] == "epiq guide"
    assert envelope["data"]["topic"] == "git"
    assert envelope["data"]["architecture"]["authority"].startswith("Git-versioned")
    assert "quickstart" in envelope["data"]["available_topics"]
    assert "schema-design" in envelope["data"]["available_topics"]
    assert ["epiq", "merge-check"] in envelope["data"]["guidance"]["commands"]


def test_guide_rejects_unknown_topic_with_available_topics() -> None:
    with pytest.raises(EpiqError) as error:
        guide_data("unknown")

    assert error.value.code == "guide_topic_not_found"
    assert "quickstart" in str(error.value.suggestion)


def test_capabilities_advertise_ledger_commands_and_schemas() -> None:
    result = capabilities()
    ledger_commands = {item["name"] for item in result["ledger_commands"]}

    assert {"kind", "record", "remote", "benchmark"} <= ledger_commands
    assert result["semantics"]["source_of_truth"].startswith("Git-versioned")
    assert "ledger-operation" in capabilities_data()["schemas"]
    assert schema_data("ledger-transaction")["name"] == "ledger-transaction-1.schema.json"
    assert capabilities("remote")["commands"][0]["architecture"] == "git-ledger"
    assert "remote" in capabilities("remote")["commands"][0]["usage"]


def test_packaged_agent_documents_no_longer_select_sqlite() -> None:
    for name in ("agent-interface", "workflow"):
        markdown = docs_show_data(name)["markdown"]
        assert "Select one SQLite" not in markdown
        assert "epiq use research.sqlite" not in markdown


@pytest.mark.parametrize("pattern", ["faculty", "literature", "startups"])
def test_schema_plans_are_review_first_and_generate_typed_commands(pattern: str) -> None:
    plan = schema_plan(pattern)

    assert plan["review_before_apply"] is True
    assert plan["inclusion_rule"]
    assert plan["identity"]
    assert len(plan["pilot_cases"]) >= 5
    assert plan["commands"]["kinds"]
    assert all("--idempotency-key" in command for command in plan["commands"]["questions"])


def test_schema_plan_models_relationships_and_repeated_events(capsys) -> None:
    main(["schema-plan", "startups"])
    plan = json.loads(capsys.readouterr().out)["data"]

    questions = {(item["kind"], item["name"]): item for item in plan["questions"]}
    assert questions[("Company", "founders")]["value_type"] == "Ref[Founder]"
    assert questions[("FundingRound", "amount")]["value_type"] == "Quantity[USD]"
    assert "FundingRound" in plan["kinds"]
