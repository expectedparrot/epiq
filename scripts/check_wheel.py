"""Exercise the installed public API and CLI without importing the source checkout."""

import argparse
import json
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

import epiq


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", action="store_true", help="Require packaged browser assets")
    args = parser.parse_args()
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "smoke.sqlite"
        database = epiq.open(path, create=True, name="Installed wheel")
        database.upsert_entity("Item", "Example")
        database.define_question("score", "Item", "Int")
        database.batch_write(
            [
                {
                    "op": "evidence.add",
                    "ref": "source",
                    "url": "urn:epiq:report",
                    "source_type": "report",
                    "title": "Scores",
                    "retrieved_at": "2026-01-01",
                    "excerpt": "The score is 5.",
                },
                {
                    "op": "claim.assert",
                    "subject": "Example",
                    "question": "score",
                    "value": 5,
                    "valid_from": "2026-01-01",
                    "evidence_refs": ["source"],
                },
            ]
        )
        assert database.claims(kind="Item")[0]["value"] == 5
        result = subprocess.run(
            [sys.executable, "-m", "epiq", "--db", str(path), "doctor"],
            cwd=temporary,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["status"] == "ok"
    if args.web:
        assert files("epiq").joinpath("_frontend/index.html").is_file()
    print("Installed wheel smoke checks passed")


if __name__ == "__main__":
    main()
