import json
from pathlib import Path

import pytest

from epiq.cli import main
from epiq.edsl_export import projection_records, write_edsl
from epiq.store import Store


def populated_projection(tmp_path: Path) -> dict:
    store = Store(tmp_path / "export.sqlite")
    store.initialize("EDSL export")
    company = store.add_entity("Company", "Acme", {}, "test")
    founder = store.add_entity("Person", "Ada", {}, "test")
    store.add_question("employees", "Company", "Int", {"label": "Employees"}, "test")
    store.add_question(
        "founders",
        "Company",
        "Ref[Person]",
        {"label": "Founders", "cardinality": "many"},
        "test",
    )
    _, evidence = store.add_evidence(
        "https://example.test/acme",
        "Acme profile",
        "2026-08-17",
        "Acme has 42 employees and was founded by Ada.",
        "test",
    )
    store.assert_claim(company, "employees", 42, "2026-08-17", evidence, "test")
    store.assert_claim(company, "founders", founder, "2026-08-17", evidence, "test")
    return store.matrix("Company")


def test_projection_records_preserve_identity_types_and_reference_names(tmp_path: Path) -> None:
    records, codebook = projection_records(populated_projection(tmp_path))
    assert records[0]["entity_name"] == "Acme"
    assert records[0]["employees"] == 42
    assert records[0]["founders"] == ["Ada"]
    assert records[0]["epiq_entity_id"].startswith("ent_")
    assert codebook["employees"] == "Employees"


@pytest.mark.parametrize("object_type", ["scenario-list", "agent-list"])
def test_native_edsl_packages_round_trip(tmp_path: Path, object_type: str) -> None:
    edsl = pytest.importorskip("edsl")
    output = write_edsl(
        populated_projection(tmp_path),
        tmp_path / f"companies.{object_type}.ep",
        object_type,
    )
    assert output.read_bytes().startswith(b"PK")
    if object_type == "scenario-list":
        loaded = edsl.ScenarioList.load(output)
        assert loaded[0]["entity_name"] == "Acme"
        assert loaded[0]["founders"] == ["Ada"]
    else:
        loaded = edsl.AgentList.load(output)
        assert loaded[0].name == "Acme"
        assert loaded[0].traits["employees"] == 42


def test_unified_cli_exports_edsl_excel_and_sqlite(tmp_path: Path, capsys) -> None:
    pytest.importorskip("edsl")
    database = tmp_path / "source.sqlite"
    store = Store(database)
    store.initialize("Exports")
    store.add_entity("Company", "Acme", {}, "test")

    agent_list = tmp_path / "agent_list.ep"
    main(
        [
            "--db",
            str(database),
            "export",
            "--format",
            "agent-list",
            "--kind",
            "Company",
            "--output-path",
            str(agent_list),
        ]
    )
    assert json.loads(capsys.readouterr().out)["data"]["format"] == "agent-list"
    assert agent_list.read_bytes().startswith(b"PK")

    workbook = tmp_path / "audit.xlsx"
    main(
        [
            "--db",
            str(database),
            "export",
            "--format",
            "xlsx",
            "--kind",
            "Company",
            "--output-path",
            str(workbook),
        ]
    )
    assert json.loads(capsys.readouterr().out)["data"]["format"] == "xlsx"
    assert workbook.read_bytes().startswith(b"PK")

    backup = tmp_path / "backup.sqlite"
    main(
        [
            "--db",
            str(database),
            "export",
            "--format",
            "sqlite",
            "--output-path",
            str(backup),
        ]
    )
    assert json.loads(capsys.readouterr().out)["data"]["format"] == "sqlite"
    assert Store(backup).doctor()["ok"] is True
