"""Verify the paper's actual CLI snippets and their advertised epistemic semantics."""

import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path


def test_paper_walkthrough_preserves_states_and_history(tmp_path):
    repository = Path(__file__).parents[1]
    paper = repository / "docs/epiq-short-paper.md"
    example_sessions = runpy.run_path(
        str(repository / "scripts/check_markdown_examples.py")
    )["example_sessions"]
    sessions = example_sessions(paper.read_text())
    assert len(sessions) == 1
    blocks = sessions[0]
    assert len(blocks) == 5
    executable = shutil.which("epiq")
    assert executable is not None
    database = tmp_path / "paper.sqlite"
    program = []
    for index, block in enumerate(blocks):
        program.extend(
            [block, f'epiq matrix --kind Company > "$EPIQ_DB.stage-{index}.json"']
        )
    program.extend(
        [
            'epiq matrix --kind Company --known-at "$paper_known_at" '
            '> "$EPIQ_DB.historical.json"',
            'epiq history > "$EPIQ_DB.history.json"',
            'epiq doctor > "$EPIQ_DB.doctor.json"',
        ]
    )
    run = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", "\n\n".join(program)],
        cwd=tmp_path,
        env={**os.environ, "EPIQ_EXAMPLE_DB": str(database)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr

    def data(suffix):
        result = json.loads(Path(f"{database}.{suffix}.json").read_text())
        assert result["status"] == "ok"
        return result["data"]

    def cell(suffix):
        return data(suffix)["rows"][0]["cells"]["supports_sso"]

    stages = [cell(f"stage-{index}") for index in range(5)]
    assert [stage["state"] for stage in stages] == [
        "Unasked", "NotFound", "Answered", "Contested", "Answered"
    ]
    assert [stage["values"] for stage in stages[:3]] == [[], [], [True]]
    assert set(stages[3]["values"]) == {True, False}
    assert stages[4]["values"] == [True]
    assert cell("historical") == stages[3]
    assert len(stages[3]["lineage"]) == 2
    assert {item["actor"] for item in stages[3]["lineage"]} == {"agent:example"}
    assert {item["source"]["url"] for item in stages[3]["lineage"]} == {
        "https://example.test/acorn/security",
        "https://example.test/comparison/acorn",
    }
    assert stages[4]["lineage"] == stages[2]["lineage"]
    history = data("history")
    assert sum(event["event_type"] == "research.not_found" for event in history) == 1
    assert sum(event["event_type"] == "claim.assert" for event in history) == 2
    retraction = [event for event in history if event["event_type"] == "claim.retracted"]
    assert len(retraction) == 1
    assert retraction[0]["actor"] == "human:reviewer"
    assert data("doctor")["ok"] is True
    assert not (tmp_path / ".epiq/config.json").exists()
