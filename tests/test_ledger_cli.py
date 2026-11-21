import json
from pathlib import Path

from epiq.cli import main


def data(capsys) -> dict:
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    return payload["data"]


def test_ledger_cli_initializes_validates_and_rebuilds(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "--actor",
            "human:test",
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--project-id",
            "prj_cli",
            "--replica-id",
            "rep_cli",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    initialized = data(capsys)
    assert initialized["ledger"]["transactions"] == 1
    assert initialized["projection"]["mode"] == "rebuild"

    main(["ledger", "validate", "--path", str(root)])
    assert data(capsys)["operations"] == 1

    main(["ledger", "status", "--path", str(root)])
    assert data(capsys)["state"] == "ready"

    (root / ".epiq" / "projection.sqlite").unlink()
    main(["ledger", "build", "--path", str(root), "--verify"])
    rebuilt = data(capsys)
    assert rebuilt["verified"] is True
    assert rebuilt["transactions"] == 1


def test_ledger_cli_command_name_includes_nested_action(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "epiq ledger init"


def test_ledger_cli_transacts_logs_retries_and_reports_conflicts(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    data(capsys)
    transaction = tmp_path / "transaction.json"
    transaction.write_text(
        json.dumps(
            {
                "operations": [
                    {"type": "entity_kind.define", "payload": {"kind": "Item"}},
                    {
                        "type": "entity.create",
                        "payload": {
                            "entity_id": "ent_a",
                            "kind": "Item",
                            "name": "Same",
                        },
                    },
                    {
                        "type": "entity.create",
                        "payload": {
                            "entity_id": "ent_b",
                            "kind": "Item",
                            "name": "Same",
                        },
                    },
                ]
            }
        )
    )
    arguments = [
        "--actor",
        "agent:test",
        "ledger",
        "transact",
        "--path",
        str(root),
        "--input",
        str(transaction),
        "--recorded-at",
        "2026-11-21T12:01:00Z",
        "--idempotency-key",
        "entities",
    ]
    main(arguments)
    created = data(capsys)
    assert created["created"] is True
    transaction_id = created["transaction"]["transaction_id"]

    main(arguments)
    retried = data(capsys)
    assert retried["created"] is False
    assert retried["transaction"]["transaction_id"] == transaction_id

    main(["ledger", "log", "--path", str(root)])
    log = data(capsys)
    assert log["count"] == 2
    assert log["transactions"][-1]["operations"] == [
        "entity_kind.define",
        "entity.create",
        "entity.create",
    ]

    main(["ledger", "conflicts", "--path", str(root)])
    conflicts = data(capsys)
    assert conflicts["count"] == 1
    assert conflicts["conflicts"][0]["conflict_type"] == "entity_identity"

    main(["ledger", "doctor", "--path", str(root)])
    doctor = data(capsys)
    assert doctor["healthy"] is True
    assert doctor["clean"] is False
    assert {item["name"]: item for item in doctor["checks"]}["domain_conflicts"] == {
        "name": "domain_conflicts",
        "status": "warn",
        "count": 1,
    }

    main(["ledger", "merge-check", "--path", str(root)])
    merge = data(capsys)
    assert merge["mergeable"] is True
    assert merge["clean"] is False
    assert merge["build_mode"] == "rebuild"


def test_ledger_cli_runs_complete_research_workflow(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    data(capsys)
    main(["ledger", "kind", "--path", str(root), "Company", "--idempotency-key", "kind"])
    data(capsys)
    main(
        [
            "ledger",
            "entity",
            "--path",
            str(root),
            "Company",
            "Acme",
            "--idempotency-key",
            "entity",
        ]
    )
    entity_id = data(capsys)["entity_id"]
    main(
        [
            "ledger",
            "question",
            "--path",
            str(root),
            "employees",
            "--for",
            "Company",
            "--type",
            "Int",
            "--idempotency-key",
            "question",
        ]
    )
    question_id = data(capsys)["question_id"]
    record_args = [
        "ledger",
        "record",
        "--path",
        str(root),
        "--subject-id",
        entity_id,
        "--question-id",
        question_id,
        "--value",
        "12",
        "--valid-from",
        "2026-11-21",
        "--source-title",
        "Acme profile",
        "--source-type",
        "web",
        "--url",
        "https://example.test/acme",
        "--retrieved-at",
        "2026-11-21",
        "--excerpt",
        "Acme has 12 employees.",
        "--idempotency-key",
        "record",
    ]
    main(record_args)
    first = data(capsys)
    main(record_args)
    assert data(capsys)["created"] is False

    main(["ledger", "claims", "--path", str(root), "--kind", "Company"])
    claims = data(capsys)
    assert claims["count"] == 1
    assert claims["claims"][0]["claim_id"] == first["claim_id"]

    main(["ledger", "matrix", "--path", str(root), "--kind", "Company"])
    matrix = data(capsys)
    assert matrix["cells"][entity_id][question_id][0]["value"] == 12

    for arguments in (
        [
            "ledger",
            "assess-evidence",
            "--path",
            str(root),
            first["evidence_id"],
            "--status",
            "accepted",
            "--reason",
            "Checked",
        ],
        [
            "ledger",
            "review-claim",
            "--path",
            str(root),
            first["claim_id"],
            "--decision",
            "accepted",
            "--reason",
            "Checked",
        ],
        [
            "ledger",
            "retire-question",
            "--path",
            str(root),
            question_id,
            "--reason",
            "Redesign",
        ],
        [
            "ledger",
            "restore-question",
            "--path",
            str(root),
            question_id,
            "--reason",
            "Keep",
        ],
    ):
        main(arguments)
        data(capsys)

    main(
        [
            "ledger",
            "challenge-question",
            "--path",
            str(root),
            question_id,
            "--problem",
            "definition_ambiguity",
            "--explanation",
            "Clarify whether contractors count",
        ]
    )
    challenge_id = data(capsys)["challenge_id"]
    main(["ledger", "question-challenges", "--path", str(root), "--status", "open"])
    assert data(capsys)["challenges"][0]["challenge_id"] == challenge_id
    main(
        [
            "ledger",
            "resolve-challenge",
            "--path",
            str(root),
            challenge_id,
            "--status",
            "resolved",
            "--resolution",
            "Employees excludes contractors",
        ]
    )
    data(capsys)
    main(
        [
            "ledger",
            "retract-claim",
            "--path",
            str(root),
            first["claim_id"],
            "--reason",
            "Outdated",
        ]
    )
    data(capsys)
    main(
        [
            "ledger",
            "not-found",
            "--path",
            str(root),
            "--subject-id",
            entity_id,
            "--question-id",
            question_id,
            "--query",
            "Acme current employees",
            "--notes",
            "No current source found",
        ]
    )
    assert data(capsys)["state"] == "NotFound"

    (root / ".epiq" / "projection.sqlite").unlink()
    main(["ledger", "build", "--path", str(root), "--verify"])
    assert data(capsys)["verified"] is True
    main(["ledger", "claims", "--path", str(root), "--include-closed"])
    assert data(capsys)["claims"][0]["reviews"][0]["decision"] == "accepted"


def test_ledger_cli_merges_entities_and_evolves_questions_atomically(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "research"
    main(
        [
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    data(capsys)
    main(["ledger", "kind", "--path", str(root), "Company"])
    data(capsys)
    entity_ids = []
    for name in ("Acme", "ACME"):
        main(["ledger", "entity", "--path", str(root), "Company", name])
        entity_ids.append(data(capsys)["entity_id"])
    main(
        [
            "ledger",
            "alias-entity",
            "--path",
            str(root),
            entity_ids[1],
            "Acme Corporation",
        ]
    )
    data(capsys)
    main(
        [
            "ledger",
            "merge-entities",
            "--path",
            str(root),
            entity_ids[1],
            entity_ids[0],
            "--reason",
            "Duplicate",
        ]
    )
    assert data(capsys)["entity_id"] == entity_ids[0]

    main(
        [
            "ledger",
            "question",
            "--path",
            str(root),
            "headcount",
            "--for",
            "Company",
            "--type",
            "Int",
        ]
    )
    predecessor = data(capsys)["question_id"]
    replacement = json.dumps(
        {
            "name": "employees",
            "subject_kind": "Company",
            "value_type": "Int",
            "definition": {"label": "Employees"},
        }
    )
    evolve_args = [
        "ledger",
        "evolve-question",
        "--path",
        str(root),
        predecessor,
        "--replacement",
        replacement,
        "--relationship",
        "refines",
        "--reason",
        "Clarified semantics",
        "--idempotency-key",
        "evolve-headcount",
    ]
    main(evolve_args)
    evolved = data(capsys)
    main(evolve_args)
    assert data(capsys)["created"] is False

    main(["ledger", "matrix", "--path", str(root), "--kind", "Company"])
    matrix = data(capsys)
    assert [item["question_id"] for item in matrix["questions"]] == evolved[
        "successor_question_ids"
    ]
    main(["ledger", "log", "--path", str(root)])
    assert data(capsys)["transactions"][-1]["operations"] == [
        "question.define",
        "question.evolve",
        "question.retire",
    ]


def test_ledger_cli_records_derivations_and_proposal_reviews(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    data(capsys)
    main(["ledger", "kind", "--path", str(root), "Item"])
    data(capsys)
    main(["ledger", "entity", "--path", str(root), "Item", "One"])
    entity_id = data(capsys)["entity_id"]
    question_ids = []
    for name in ("input_value", "derived_value"):
        main(
            [
                "ledger",
                "question",
                "--path",
                str(root),
                name,
                "--for",
                "Item",
                "--type",
                "Int",
            ]
        )
        question_ids.append(data(capsys)["question_id"])
    main(
        [
            "ledger",
            "record",
            "--path",
            str(root),
            "--subject-id",
            entity_id,
            "--question-id",
            question_ids[0],
            "--value",
            "2",
            "--valid-from",
            "2026-11-21",
            "--source-title",
            "Source",
            "--source-type",
            "web",
            "--url",
            "https://example.test",
            "--retrieved-at",
            "2026-11-21",
            "--excerpt",
            "Value is two.",
        ]
    )
    recorded = data(capsys)
    derive_args = [
        "ledger",
        "derive",
        "--path",
        str(root),
        "--subject-id",
        entity_id,
        "--question-id",
        question_ids[1],
        "--value",
        "4",
        "--valid-from",
        "2026-11-21",
        "--evidence-id",
        recorded["evidence_id"],
        "--operation",
        "double",
        "--input-claim-id",
        recorded["claim_id"],
        "--parameters",
        '{"factor":2}',
        "--idempotency-key",
        "derive-double",
    ]
    main(derive_args)
    derived = data(capsys)
    main(derive_args)
    assert data(capsys)["created"] is False
    main(["ledger", "derivations", "--path", str(root)])
    lineage = data(capsys)["derivations"][0]
    assert lineage["claim_id"] == derived["claim_id"]
    assert lineage["inputs"] == {"operand": [recorded["claim_id"]]}

    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text('{"operations":[{"type":"entity.alias"}]}')
    main(
        [
            "ledger",
            "propose",
            "--path",
            str(root),
            "--input",
            str(proposal_path),
            "--idempotency-key",
            "proposal-one",
        ]
    )
    proposal_id = data(capsys)["proposal_id"]
    main(
        [
            "ledger",
            "review-proposal",
            "--path",
            str(root),
            proposal_id,
            "--decision",
            "accepted",
            "--reason",
            "Looks good",
        ]
    )
    data(capsys)
    main(["ledger", "proposals", "--path", str(root)])
    proposal = data(capsys)["proposals"][0]
    assert proposal["document"] == {"operations": [{"type": "entity.alias"}]}
    assert proposal["reviews"][0]["decision"] == "accepted"


def test_ledger_cli_retains_artifacts_bundles_and_reports(tmp_path: Path, capsys) -> None:
    root = tmp_path / "research"
    main(
        [
            "ledger",
            "init",
            "--path",
            str(root),
            "--name",
            "Research",
            "--recorded-at",
            "2026-11-21T12:00:00Z",
        ]
    )
    initialized = data(capsys)
    genesis_id = initialized["ledger"]["frontier"][0]
    report_path = tmp_path / "report.md"
    report_path.write_text("# Findings\n\nEvidence-backed report.\n")
    main(
        [
            "ledger",
            "artifact-add",
            "--path",
            str(root),
            "--input",
            str(report_path),
        ]
    )
    artifact = data(capsys)
    main(
        [
            "ledger",
            "artifact-verify",
            "--path",
            str(root),
            "--digest",
            artifact["digest"],
        ]
    )
    assert data(capsys)["count"] == 1

    request_path = tmp_path / "request.json"
    request_path.write_text('{"query":"example"}')
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(
        json.dumps({"documents": [{"artifact_digest": artifact["digest"]}]})
    )
    main(
        [
            "ledger",
            "bundle-record",
            "--path",
            str(root),
            "--job-id",
            "job_one",
            "--request",
            str(request_path),
            "--bundle",
            str(bundle_path),
            "--idempotency-key",
            "bundle-one",
        ]
    )
    bundle_id = data(capsys)["bundle_id"]
    report_args = [
        "ledger",
        "report-record",
        "--path",
        str(root),
        "--type",
        "summary",
        "--input",
        str(report_path),
        "--input-transaction-id",
        genesis_id,
        "--parameters",
        '{"format":"markdown"}',
        "--idempotency-key",
        "report-one",
    ]
    main(report_args)
    report = data(capsys)
    main(report_args)
    assert data(capsys)["created"] is False

    (root / ".epiq" / "projection.sqlite").unlink()
    main(["ledger", "bundles", "--path", str(root)])
    assert data(capsys)["bundles"][0]["bundle_id"] == bundle_id
    main(["ledger", "reports", "--path", str(root)])
    projected = data(capsys)["reports"][0]
    assert projected["report_id"] == report["report_id"]
    assert projected["content_digest"] == artifact["digest"]
    main(["ledger", "doctor", "--path", str(root)])
    checks = {item["name"]: item for item in data(capsys)["checks"]}
    assert checks["retained_artifacts"] == {
        "name": "retained_artifacts",
        "status": "pass",
        "count": 1,
    }
