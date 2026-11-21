# Epiq workflow

Select or initialize one Git-ledger project, define its entity kinds and typed questions, then append
evidence-backed claims without bypassing provenance.

Upgrade and inspect the installed CLI before relying on current commands:

```bash
uv tool upgrade epiq
epiq version
epiq guide
```

```bash
epiq --project research init --name "Research project"
epiq --project research kind Company
epiq --project research entity Company Acme
epiq --project research question employees --for Company --type Int
epiq --project research record ...
epiq --project research matrix --kind Company
```

Use IDs returned by the CLI rather than guessing them. `record` atomically captures bounded evidence
and asserts its supported answer. Use proposals for conclusions that require review and `not-found`
for an unsuccessful search, never as a negative factual answer.

Before sharing, run `epiq doctor` and `epiq merge-check`. Publish Epiq-owned files with `epiq remote
publish`; `.epiq/` remains local and disposable.
