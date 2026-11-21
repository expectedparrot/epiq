import json
from pathlib import Path

SCHEMAS = Path(__file__).parents[1] / "src" / "epiq" / "schemas"


def test_ledger_schemas_are_versioned_and_linked() -> None:
    operation = json.loads((SCHEMAS / "ledger-operation-1.schema.json").read_text())
    transaction = json.loads((SCHEMAS / "ledger-transaction-1.schema.json").read_text())

    assert operation["$id"].endswith("/ledger-operation-1.json")
    assert transaction["$id"].endswith("/ledger-transaction-1.json")
    assert transaction["properties"]["schema_version"] == {"const": "1"}
    assert transaction["properties"]["operations"]["items"] == {
        "$ref": "ledger-operation-1.schema.json"
    }


def test_operation_catalog_has_unique_versioned_names() -> None:
    operation = json.loads((SCHEMAS / "ledger-operation-1.schema.json").read_text())
    names = operation["properties"]["type"]["enum"]

    assert len(names) == len(set(names))
    assert {"entity.create", "evidence.capture", "claim.assert", "proposal.review"} <= set(
        names
    )
