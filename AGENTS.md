# Epiq coding-agent guide

Epiq's authoritative datastore is a Git-versioned directory of immutable JSON transactions.
SQLite under `.epiq/` is a disposable projection.

## Orient

Run these instead of guessing CLI arguments:

```bash
epiq capabilities
epiq guide
epiq guide workflow
epiq guide schema-design
epiq guide schema-patterns
epiq guide evidence
epiq guide git
epiq guide recovery
```

Every command returns a versioned JSON envelope. Read successful results from `data` and errors
from `errors`. Command arrays in guidance are tokenized argv, not shell strings.

## Work safely

- Select a project with `--project`, `EPIQ_PROJECT`, `epiq use-project`, or by entering a directory
  containing `epiq.toml`.
- Use IDs returned by Epiq and stable `--idempotency-key` values for automated writes.
- Record claims with bounded evidence; never invent evidence or turn an unsuccessful search into a
  negative answer.
- Never edit acknowledged transaction JSON or `.epiq/projection.sqlite` directly.
- Never commit `.epiq/`, credentials, transient progress, or unrelated workspace changes.
- After Git integration, run `epiq merge-check`, inspect `epiq conflicts`, and run `epiq doctor`.
- Use `epiq remote publish` to stage only Epiq-owned paths and optionally create a pull request.

## Develop this repository

Use `rg` for discovery and `apply_patch` for edits. Run focused tests first, then:

```bash
uv run pytest -q
uv run ruff check .
```

The older SQLite-only implementation and tutorials are legacy behavioral references. Do not add new
features there unless a task explicitly targets legacy compatibility.
