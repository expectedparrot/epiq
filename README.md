# Epiq

Epiq is a local-first epistemic database for agent-driven research. People and agents define
typed questions, collect immutable evidence, assert temporally scoped claims, and derive views
whose lineage remains inspectable. SQLite is canonical; an append-only `events` table records
every accepted change.

This repository is an executable vertical slice of the formal model in *Databases That Ask
Back*. It currently demonstrates:

- immutable entities, versioned questions, sources, evidence, claims, and events;
- valid time and transaction time on claims;
- evidence-required assertions and explicit retraction;
- deterministic time-travel queries;
- semiring-style claim tokens explaining derived results;
- a narrow, statically checked EpiQL grammar;
- concurrent-writer serialization using SQLite WAL and `BEGIN IMMEDIATE`.

It deliberately does **not** search the web or call a language model. Research agents operate
outside Epiq and submit proposed evidence and claims through its validated interface.

## Run the Patriots example

Python 3.11 or later is sufficient; there are no runtime dependencies.

```bash
python -m epiq --db /tmp/patriots.sqlite init --name "Patriots 2025"
python -m epiq --db /tmp/patriots.sqlite demo patriots
python -m epiq --db /tmp/patriots.sqlite season-record "New England Patriots 2025"
```

The final command returns `14-3` plus the 17 claim tokens from which that record was derived.
Move the transaction-time cutoff backward to see the cell change:

```bash
python -m epiq --db /tmp/patriots.sqlite season-record \
  "New England Patriots 2025" --known-at 2025-09-22T00:00:00Z
```

That query returns `1-2`: only the first three result claims were known at the selected cutoff.

Check the corresponding DSL without performing any effects:

```bash
python -m epiq --db /tmp/patriots.sqlite check examples/patriots.epiq
```

## Manual write loop

All commands emit JSON. Write commands also accept `--actor` before the subcommand.

```bash
epiq init --name "My space"
epiq entity Game "Patriots Week 1" --attributes '{"season_id":"ent_..."}'
epiq question game_result --for Game --type 'Enum[W,L,T]' \
  --definition '{"cardinality":"one"}'
epiq evidence --url https://example.test/game --title "Final" \
  --retrieved-at 2026-08-15 --excerpt "New England lost 13-20."
epiq assert --subject "Patriots Week 1" --question game_result --value L \
  --valid-from 2025-09-07 --evidence evd_...
```

Assertions without existing evidence fail. Repeating the same normalized claim is idempotent.
Corrections use `retract`; later versions will add atomic `supersede` to the CLI.

## AI-interviewer market example

The repository can translate the earlier Cham research packet into an Epiq workspace. Controlled
features become individual typed questions, making missing claims executable research gaps rather
than false values.

```bash
epiq --db examples/ai-interviewers.sqlite init --name "AI Interviewer Market"
epiq --db examples/ai-interviewers.sqlite --actor agent:corpus-import import-cham \
  --entities path/to/entities.json \
  --evidence path/to/evidence.json \
  --claims path/to/claims.json
epiq --db examples/ai-interviewers.sqlite matrix --kind Company
python scripts/expand_ai_market.py --db examples/ai-interviewers.sqlite
epiq --db examples/ai-interviewers.sqlite export-html \
  --kind Company --output examples/ai-interviewers-visual.html
```

The expansion script adds a researched discovery cohort (Glaut, Conveo, Koji Research, and
Tellet), then classifies all eight companies with typed `market_segment` and
`comparison_status` questions. The original four remain `core`; newly discovered companies are
`candidate` until their research coverage is comparable.

Open `examples/ai-interviewers-visual.html` to inspect the taxonomy, research pipeline, feature
coverage, matrix cells, generated backlog, and evidence lineage. The imported database is
generated and gitignored; the HTML projection is a reproducible report artifact.

The adapter currently uses the primary evidence item for two older multi-source claims. Native
many-to-many claim/evidence support is the next storage migration.

## Generic HTML explorer

The HTML renderer is schema-driven and works with any Epiq database. It chooses the entity kind
with the richest defined schema by default, renders every current question for that kind, and distinguishes Answered,
Contested, NotFound, and Unasked cells. Evidence and claim lineage remain inspectable in-place.

```bash
epiq --db path/to/research.sqlite export-html --output report.html
```

Use `--kind Game` (or another entity kind) when the database contains multiple populations.

The Cape Cod example exercises the same renderer with municipal statistics rather than companies:

```bash
python scripts/build_cape_cod_towns.py --db examples/cape-cod-towns.sqlite
epiq --db examples/cape-cod-towns.sqlite export-html \
  --kind Town --output examples/cape-cod-towns.html
```

Its population and median owner-occupied home values use the Census ACS 2024 five-year release;
the evidence attached to every cell retains the corresponding margin of error.

Any projection can also be exported to a native Excel workbook. The workbook contains `Data`,
`Evidence`, and `Unknowns` sheets so spreadsheet analysis does not discard provenance.

```bash
epiq --db examples/cape-cod-towns.sqlite export-xlsx \
  --kind Town \
  --output examples/cape-cod-towns.xlsx
```

## EpiQL v0.1

The parser currently accepts typed question declarations and count-over-filter derivations:

```epiq
question game_result : Enum[W,L,T] for Game {
  ask "What was the final result?"
  cardinality one
}

derive wins : Int for Season =
  games |> where game_result == W |> count
```

Unsupported derivations fail loudly. The next language increment will add populations, temporal
lenses, proposal-producing `research` effects, and atomic acceptance policies. The narrow parser
is a contract, not a mock implementation of the broader grammar.

## Development

```bash
uv sync --extra test
uv run pytest -q
uv run ruff check .
```

## Storage invariants

1. Events, sources, and evidence are never updated or deleted.
2. A claim requires an existing evidence fragment.
3. Closing a claim changes its active interval; its assertion remains addressable.
4. Replayable events and operational tables change in one SQLite transaction.
5. A pure query at fixed valid- and transaction-time cutoffs is deterministic.
6. Failure to find evidence is not a negative claim.
