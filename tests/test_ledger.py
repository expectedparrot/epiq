import json

import pytest

from epiq.errors import EpiqError
from epiq.ledger import Operation, Transaction, canonical_bytes, strict_loads


def transaction() -> Transaction:
    operation = Operation.create(
        "entity.create",
        {"entity_id": "ent_example", "kind": "Item", "name": "Café"},
        operation_id="op_01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    return Transaction.create(
        "prj_example",
        "rep_alice",
        "agent:alice",
        [operation],
        transaction_id="txn_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        recorded_at="2026-11-21T11:30:00-05:00",
        idempotency_key="entity-example",
    )


def test_transaction_round_trip_is_canonical_and_normalizes_time() -> None:
    item = transaction()

    assert item.recorded_at == "2026-11-21T16:30:00.000000Z"
    assert item.relative_path().as_posix() == (
        "ledger/transactions/01/txn_01ARZ3NDEKTSV4RRFFQ69G5FAV.json"
    )
    assert item.canonical().endswith(b"\n")
    assert b'"name":"Caf\xc3\xa9"' in item.canonical()
    assert Transaction.loads(item.canonical()) == item


def test_digest_detects_tampering() -> None:
    document = transaction().as_dict()
    document["operations"][0]["payload"]["name"] = "Changed"

    with pytest.raises(EpiqError, match="digest does not match") as error:
        Transaction.from_dict(document)

    assert error.value.code == "ledger_digest_mismatch"


def test_strict_json_rejects_duplicates_and_nonfinite_numbers() -> None:
    with pytest.raises(EpiqError) as duplicate:
        strict_loads('{"actor":"a","actor":"b"}')
    assert duplicate.value.code == "duplicate_json_key"

    with pytest.raises(EpiqError) as nonfinite:
        strict_loads('{"value":NaN}')
    assert nonfinite.value.code == "invalid_json_number"


def test_canonical_encoding_is_stable_and_rejects_non_json_values() -> None:
    first = {"z": [1, 1.5, True], "a": {"é": "value"}}
    second = json.loads('{"a":{"é":"value"},"z":[1,1.5,true]}')
    assert canonical_bytes(first) == canonical_bytes(second)

    with pytest.raises(EpiqError) as error:
        canonical_bytes({"bad": {1, 2}})
    assert error.value.code == "invalid_json_value"


def test_unknown_operations_and_duplicate_operation_ids_are_rejected() -> None:
    with pytest.raises(EpiqError) as unknown:
        Operation.create("unknown.action", {}).as_dict()
    assert unknown.value.code == "unknown_operation_type"

    operation = Operation.create(
        "entity.create", {}, operation_id="op_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    )
    with pytest.raises(EpiqError) as duplicate:
        Transaction.create(
            "prj_example",
            "rep_alice",
            "agent:alice",
            [operation, operation],
            recorded_at="2026-11-21T16:30:00Z",
        ).validate()
    assert duplicate.value.code == "duplicate_operation"
