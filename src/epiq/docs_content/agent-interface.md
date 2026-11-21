# Epiq agent interface

Epiq is agent-first. Every command emits one versioned JSON envelope to standard output. Errors
use the same envelope on standard error and exit nonzero. Treat `data` as the command result and
execute only reviewed `next_steps`.

The authoritative store is a Git-versioned directory of immutable transaction files. SQLite under
`.epiq/` is a disposable projection; never edit it or commit it. Start with:

```bash
uv tool install git+https://github.com/expectedparrot/epiq.git  # first install
uv tool upgrade epiq                                           # existing install
epiq version
```

Then orient with:

```bash
epiq version
epiq capabilities
epiq guide
epiq guide workflow
epiq --project <directory> status
```

Each next action provides an exact `argv` array and declares mutation, network, and approval
effects. Use `epiq agent schema envelope` and `epiq agent schema action` for the authoritative
contracts. Human-readable output is opt-in through `--human`, `-H`, or a supported explicit
format.

Claims require evidence. External source providers and reasoning backends propose material; only
Epiq validates and appends ledger transactions. Corrections are new operations rather than edits to
acknowledged transaction files. Use `epiq guide evidence`, `epiq guide git`, and `epiq guide
recovery` for machine-readable task guidance.
