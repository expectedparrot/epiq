import os
import shutil
import subprocess
from pathlib import Path

from epiq.store import Store


def test_all_cli_only_examples_build_and_pass_integrity(tmp_path: Path) -> None:
    executable = shutil.which("epiq")
    assert executable is not None
    repository = Path(__file__).parents[1]
    environment = {**os.environ, "EPIQ_BIN": executable}

    subprocess.run(
        [str(repository / "examples/cli/build-all.sh"), str(tmp_path)],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    expectations = {
        "hiring-committee.sqlite": ("Candidate", 2),
        "investment-opportunities.sqlite": ("Company", 3),
        "competitor-features.sqlite": ("Product", 3),
        "public-figure-writing.sqlite": ("Work", 3),
    }
    for filename, (entity_kind, row_count) in expectations.items():
        store = Store(tmp_path / filename)
        assert store.doctor()["ok"] is True
        assert len(store.matrix(entity_kind)["rows"]) == row_count
