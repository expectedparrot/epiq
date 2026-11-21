import json
from pathlib import Path

import pytest

from epiq.ledger_api import LedgerDatabase
from epiq.ledger_exports import export_ledger


def populated(tmp_path: Path) -> LedgerDatabase:
    database = LedgerDatabase(
        tmp_path / "project",
        create=True,
        name="Exports",
        project_id="prj_exports",
        replica_id="rep_exports",
        clock=lambda: "2026-11-21T12:00:00Z",
    )
    database.define_kind("Company")
    database.define_kind("Person")
    company = database.create_entity("Company", "Acme")["entity_id"]
    founder = database.create_entity("Person", "Ada")["entity_id"]
    employees = database.define_question(
        "employees", "Company", "Int", definition={"label": "Employees"}
    )["question_id"]
    founders = database.define_question(
        "founders",
        "Company",
        "Ref[Person]",
        definition={"label": "Founders", "cardinality": "many"},
    )["question_id"]
    logo = database.define_question(
        "logo", "Company", "Image", definition={"label": "Logo"}
    )["question_id"]
    evidence = database.capture_evidence(
        source_title="Acme",
        source_type="web",
        url="https://example.test",
        retrieved_at="2026-11-21",
        excerpt="Acme has 42 employees and was founded by Ada.",
    )["evidence_id"]
    database.assert_claim(
        company,
        employees,
        42,
        valid_from="2026-11-21",
        evidence_ids=[evidence],
    )
    database.assert_claim(
        company,
        founders,
        [founder],
        valid_from="2026-11-21",
        evidence_ids=[evidence],
    )
    database.assert_claim(
        company,
        logo,
        "https://images.example/acme.png",
        valid_from="2026-11-21",
        evidence_ids=[evidence],
    )
    return database


@pytest.mark.parametrize(
    ("export_format", "suffix"),
    [
        ("matrix-json", ".json"),
        ("matrix-csv", ".csv"),
        ("claims-jsonl", ".jsonl"),
        ("claims-csv", ".csv"),
    ],
)
def test_ledger_exports_are_byte_deterministic(
    tmp_path: Path, export_format: str, suffix: str
) -> None:
    database = populated(tmp_path)
    first = export_ledger(database, tmp_path / f"first{suffix}", export_format, kind="Company")
    second = export_ledger(database, tmp_path / f"second{suffix}", export_format, kind="Company")
    assert first.read_bytes() == second.read_bytes()

    (database.path / ".epiq" / "projection.sqlite").unlink()
    third = export_ledger(database, tmp_path / f"rebuilt{suffix}", export_format, kind="Company")
    assert first.read_bytes() == third.read_bytes()


@pytest.mark.parametrize("object_type", ["scenario-list", "agent-list"])
def test_ledger_exports_native_edsl_objects(tmp_path: Path, object_type: str) -> None:
    edsl = pytest.importorskip("edsl")
    database = populated(tmp_path)
    output = export_ledger(
        database,
        tmp_path / f"companies.{object_type}.ep",
        object_type,
        kind="Company",
    )
    assert output.read_bytes().startswith(b"PK")
    if object_type == "scenario-list":
        loaded = edsl.ScenarioList.load(output)
        record = dict(loaded[0])
    else:
        loaded = edsl.AgentList.load(output)
        record = loaded[0].traits
    assert record["employees"] == 42
    assert record["founders"] == ["Ada"]


def test_matrix_json_is_lossless_claim_projection(tmp_path: Path) -> None:
    database = populated(tmp_path)
    output = export_ledger(database, tmp_path / "matrix.json", "matrix-json", kind="Company")
    document = json.loads(output.read_text())
    company_id = document["entities"][0]["entity_id"]
    employees_id = next(
        item["question_id"] for item in document["questions"] if item["name"] == "employees"
    )
    assert document["cells"][company_id][employees_id][0]["value"] == 42


def test_ledger_html_is_self_contained_deterministic_and_evidence_backed(
    tmp_path: Path,
) -> None:
    database = populated(tmp_path)
    first = export_ledger(database, tmp_path / "first.html", "html", kind="Company")
    content = first.read_text()

    assert "<!doctype html>" in content
    assert "Acme" in content
    assert "Acme has 42 employees and was founded by Ada." in content
    assert "https://example.test" in content
    assert "ledger-1.0" in content
    assert "Portable snapshot · read only" in content
    assert 'id="table-list"' in content
    assert 'id="table-select"' not in content
    assert "No database connection" not in content
    assert '"value_type":"Image"' in content
    assert '"value":"https://images.example/acme.png"' in content
    assert "q.value_type==='Image'" in content

    (database.path / ".epiq" / "projection.sqlite").unlink()
    rebuilt = export_ledger(database, tmp_path / "rebuilt.html", "html", kind="Company")
    assert rebuilt.read_bytes() == first.read_bytes()
