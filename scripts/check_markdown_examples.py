#!/usr/bin/env python3
"""Execute marked bash fences, optionally sharing one session per document."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

BLOCK = re.compile(r"<!--\s*epiq-example\s*-->\s*```bash\n(.*?)\n```", re.DOTALL)


def example_sessions(content: str) -> list[list[str]]:
    """Keep opt-in sequential tutorials in one shell; other blocks stay isolated."""
    blocks = BLOCK.findall(content)
    if "<!-- epiq-example-session -->" in content:
        return [blocks] if blocks else []
    return [[block] for block in blocks]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    root = Path(__file__).parents[1]
    paths = args.paths or [
        *sorted((root / "examples/cli").glob("*/README.md")),
        root / "docs/epiq-short-paper.md",
    ]
    executable = shutil.which("epiq")
    if executable is None:
        raise SystemExit("epiq executable not found on PATH")

    executed = 0
    with tempfile.TemporaryDirectory(prefix="epiq-markdown-") as directory:
        temporary = Path(directory)
        # Tutorials use `epiq use`, which writes workspace-local selection state. Keep that state
        # out of the repository while retaining the examples/ paths used by their build commands.
        (temporary / "examples").symlink_to(root / "examples", target_is_directory=True)
        for path in paths:
            resolved = path if path.is_absolute() else root / path
            for index, blocks in enumerate(example_sessions(resolved.read_text())):
                database = temporary / f"{resolved.parent.name}-{index}.sqlite"
                environment = {
                    **os.environ,
                    "EPIQ_BIN": executable,
                    "EPIQ_EXAMPLE_DB": str(database),
                }
                subprocess.run(
                    ["bash", "-eu", "-o", "pipefail", "-c", "\n\n".join(blocks)],
                    cwd=temporary,
                    env=environment,
                    check=True,
                )
                executed += len(blocks)
    if executed == 0:
        raise SystemExit("no executable Markdown examples found")
    print(f"Executed {executed} Markdown example blocks")


if __name__ == "__main__":
    main()
