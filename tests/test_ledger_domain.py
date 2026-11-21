import sqlite3
from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.ledger import LedgerRepository, Operation, Transaction
from epiq.ledger_projection import LedgerProjector


def project(tmp_path: Path) -> tuple[LedgerRepository, LedgerProjector]:
    repository = LedgerRepository.initialize(
        tmp_path / "domain",
        "Domain",
        actor="human:test",
        recorded_at="2026-11-21T12:00:00Z",
        project_id="prj_domain",
        replica_id="rep_domain",
    )
    return repository, LedgerProjector(repository)


def commit(
    repository: LedgerRepository,
    transaction_id: str,
    operations: list[Operation],
    *,
    parents: tuple[str, ...] | None = None,
) -> Transaction:
    transaction = Transaction.create(
        repository.config.project_id,
        repository.config.replica_id,
        "agent:test",
        operations,
        transaction_id=transaction_id,
        recorded_at="2026-11-21T12:01:00Z",
        parents=repository.frontier() if parents is None else parents,
    )
    repository.append(transaction)
    return transaction


def rows(projector: LedgerProjector, query: str) -> list[tuple]:
    with sqlite3.connect(projector.path) as connection:
        return list(connection.execute(query))


def test_projects_entities_and_questions_rebuild_from_domain_operations(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [
            Operation.create("entity_kind.define", {"kind": "Item"}),
            Operation.create(
                "entity.create",
                {
                    "entity_id": "ent_example",
                    "kind": "Item",
                    "name": "Example",
                    "attributes": {"source": "test"},
                },
            ),
            Operation.create(
                "question.define",
                {
                    "question_id": "q_verdict",
                    "name": "verdict",
                    "subject_kind": "Item",
                    "value_type": "Enum[pass,fail]",
                    "definition": {"label": "Verdict"},
                },
            ),
        ],
    )

    projector.build()
    assert rows(projector, "SELECT kind,name FROM projected_entities") == [("Item", "Example")]
    assert rows(
        projector, "SELECT name,value_type FROM projected_questions"
    ) == [("verdict", "Enum[pass,fail]")]
    assert rows(projector, "SELECT * FROM projected_conflicts") == []

    before = rows(projector, "SELECT * FROM projected_entities")
    projector.path.unlink()
    projector.build()
    assert rows(projector, "SELECT * FROM projected_entities") == before


def test_concurrent_entity_and_question_identity_conflicts_are_preserved(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    kind = commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [Operation.create("entity_kind.define", {"kind": "Item"})],
    )
    parents = (kind.transaction_id,)
    for transaction_id, entity_id, question_id in (
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAX", "ent_a", "q_a"),
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAY", "ent_b", "q_b"),
    ):
        commit(
            repository,
            transaction_id,
            [
                Operation.create(
                    "entity.create",
                    {
                        "entity_id": entity_id,
                        "kind": "Item",
                        "name": "Same item",
                        "identity": {"external_id": "same"},
                    },
                ),
                Operation.create(
                    "question.define",
                    {
                        "question_id": question_id,
                        "name": "verdict",
                        "subject_kind": "Item",
                        "value_type": "Bool",
                    },
                ),
            ],
            parents=parents,
        )

    projector.build()
    conflicts = rows(
        projector,
        "SELECT conflict_type,object_ids_json FROM projected_conflicts ORDER BY conflict_type",
    )
    assert conflicts == [
        ("entity_identity", '["ent_a","ent_b"]'),
        ("question_definition", '["q_a","q_b"]'),
    ]


def test_entity_merge_conflict_is_visible_but_cycle_rejects_projection(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    base = commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [
            Operation.create("entity_kind.define", {"kind": "Item"}),
            *[
                Operation.create(
                    "entity.create",
                    {"entity_id": f"ent_{name}", "kind": "Item", "name": name},
                )
                for name in ("a", "b", "c")
            ],
        ],
    )
    parents = (base.transaction_id,)
    for transaction_id, destination in (
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAX", "ent_b"),
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAY", "ent_c"),
    ):
        commit(
            repository,
            transaction_id,
            [
                Operation.create(
                    "entity.merge",
                    {
                        "source_entity_id": "ent_a",
                        "destination_entity_id": destination,
                        "reason": "duplicate",
                    },
                )
            ],
            parents=parents,
        )
    projector.build()
    assert rows(projector, "SELECT conflict_type FROM projected_conflicts") == [
        ("entity_merge",)
    ]

    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAZ",
        [
            Operation.create(
                "entity.merge",
                {
                    "source_entity_id": "ent_b",
                    "destination_entity_id": "ent_a",
                    "reason": "bad cycle",
                },
            )
        ],
    )
    before = projector.path.read_bytes()
    with pytest.raises(EpiqError) as cycle:
        projector.build()
    assert cycle.value.code == "entity_merge_cycle"
    assert projector.path.read_bytes() == before


def test_typed_payloads_reject_unknown_or_malformed_fields() -> None:
    with pytest.raises(EpiqError) as missing:
        Operation.create("entity.create", {"entity_id": "ent_a"}).validate()
    assert missing.value.code == "invalid_operation_payload"

    with pytest.raises(EpiqError) as unknown:
        Operation.create("entity_kind.define", {"kind": "Item", "extra": True}).validate()
    assert unknown.value.code == "invalid_operation_payload"

    operation = Operation.create(
        "question.evolve",
        {
            "predecessor_question_id": "q_a",
            "successor_question_ids": ["q_b", "q_b"],
            "relationship": "replaces",
            "reason": "duplicate",
        },
    )
    with pytest.raises(EpiqError) as duplicate:
        operation.validate()
    assert duplicate.value.code == "invalid_operation_payload"


def test_concurrent_visibility_disagreement_is_explicit_and_causally_resolved(
    tmp_path: Path,
) -> None:
    repository, projector = project(tmp_path)
    base = commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [
            Operation.create("entity_kind.define", {"kind": "Item"}),
            Operation.create(
                "entity.create", {"entity_id": "ent_a", "kind": "Item", "name": "A"}
            ),
            Operation.create(
                "question.define",
                {
                    "question_id": "q_a",
                    "name": "value",
                    "subject_kind": "Item",
                    "value_type": "String",
                },
            ),
        ],
    )
    branches = []
    for transaction_id, operation_type in (
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAX", "entity.retire"),
        ("txn_01ARZ3NDEKTSV4RRFFQ69G5FAY", "entity.restore"),
    ):
        branches.append(
            commit(
                repository,
                transaction_id,
                [
                    Operation.create(
                        operation_type,
                        {"entity_id": "ent_a", "reason": "concurrent choice"},
                    ),
                    Operation.create(
                        operation_type.replace("entity.", "question."),
                        {"question_id": "q_a", "reason": "concurrent choice"},
                    ),
                ],
                parents=(base.transaction_id,),
            )
        )

    projector.build()
    assert rows(
        projector,
        "SELECT visible,conflicted FROM projected_entity_visibility_state",
    ) == [(None, 1)]
    assert rows(projector, "SELECT conflict_type FROM projected_conflicts") == [
        ("entity_visibility",),
        ("question_visibility",),
    ]
    assert rows(
        projector,
        "SELECT visible,conflicted FROM projected_question_visibility_state",
    ) == [(None, 1)]

    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAZ",
        [
            Operation.create(
                "entity.restore", {"entity_id": "ent_a", "reason": "merge resolution"}
            ),
            Operation.create(
                "question.restore", {"question_id": "q_a", "reason": "merge resolution"}
            ),
        ],
        parents=tuple(item.transaction_id for item in branches),
    )
    projector.build()
    assert rows(
        projector,
        "SELECT visible,conflicted FROM projected_entity_visibility_state",
    ) == [(1, 0)]
    assert rows(
        projector,
        "SELECT visible,conflicted FROM projected_question_visibility_state",
    ) == [(1, 0)]
    assert rows(projector, "SELECT conflict_type FROM projected_conflicts") == []


def test_explicit_merge_and_evolution_resolve_pair_conflicts(tmp_path: Path) -> None:
    repository, projector = project(tmp_path)
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAW",
        [
            Operation.create("entity_kind.define", {"kind": "Item"}),
            Operation.create(
                "entity.create", {"entity_id": "ent_a", "kind": "Item", "name": "Same"}
            ),
            Operation.create(
                "entity.create", {"entity_id": "ent_b", "kind": "Item", "name": "Same"}
            ),
            Operation.create(
                "question.define",
                {
                    "question_id": "q_a",
                    "name": "value",
                    "subject_kind": "Item",
                    "value_type": "String",
                },
            ),
            Operation.create(
                "question.define",
                {
                    "question_id": "q_b",
                    "name": "value",
                    "subject_kind": "Item",
                    "value_type": "String",
                },
            ),
        ],
    )
    commit(
        repository,
        "txn_01ARZ3NDEKTSV4RRFFQ69G5FAX",
        [
            Operation.create(
                "entity.merge",
                {
                    "source_entity_id": "ent_b",
                    "destination_entity_id": "ent_a",
                    "reason": "same item",
                },
            ),
            Operation.create(
                "question.evolve",
                {
                    "predecessor_question_id": "q_a",
                    "successor_question_ids": ["q_b"],
                    "relationship": "replaces",
                    "reason": "new definition",
                },
            ),
        ],
    )

    projector.build()
    assert rows(projector, "SELECT * FROM projected_conflicts") == []
