import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_all_cli_only_markdown_examples_execute() -> None:
    executable = shutil.which("epiq")
    assert executable is not None
    repository = Path(__file__).parents[1]
    environment = {**os.environ, "EPIQ_BIN": executable}

    subprocess.run(
        [sys.executable, str(repository / "scripts/check_markdown_examples.py")],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
