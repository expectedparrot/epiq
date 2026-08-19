# Epiq agent interface

Epiq is agent-first. Every command emits one versioned JSON envelope to standard output. Errors
use the same envelope on standard error and exit nonzero. Treat `data` as the command result and
execute only reviewed `next_steps`.

Start with:

```bash
epiq version
epiq capabilities
epiq guide
epiq agent status
epiq next
```

Each next action provides an exact `argv` array and declares mutation, network, and approval
effects. Use `epiq agent schema envelope` and `epiq agent schema action` for the authoritative
contracts. Human-readable output is opt-in through `--human`, `-H`, or a supported explicit
format.

Claims require evidence. External source providers and reasoning backends propose material; only
Epiq validates and writes the database. Corrections are append-only events rather than edits to
history.
