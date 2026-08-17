#!/usr/bin/env python3
"""Execute bash fences immediately preceded by ``<!-- epiq-example -->``."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

BLOCK = re.compile(r"<!--\s*epiq-example\s*-->\s*```bash\n(.*?)\n```", re.DOTALL)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    root = Path(__file__).parents[1]
    paths = args.paths or sorted((root / "examples/cli").glob("*/README.md"))
    executable = shutil.which("epiq")
    if executable is None:
        raise SystemExit("epiq executable not found on PATH")

    executed = 0
    with tempfile.TemporaryDirectory(prefix="epiq-markdown-") as directory:
        temporary = Path(directory)
        for path in paths:
            resolved = path if path.is_absolute() else root / path
            blocks = BLOCK.findall(resolved.read_text())
            for index, block in enumerate(blocks):
                database = temporary / f"{resolved.parent.name}-{index}.sqlite"
                environment = {
                    **os.environ,
                    "EPIQ_BIN": executable,
                    "EPIQ_EXAMPLE_DB": str(database),
                }
                subprocess.run(
                    ["bash", "-eu", "-o", "pipefail", "-c", block],
                    cwd=root,
                    env=environment,
                    check=True,
                )
                executed += 1
    if executed == 0:
        raise SystemExit("no executable Markdown examples found")
    print(f"Executed {executed} Markdown example blocks")


if __name__ == "__main__":
    main()
