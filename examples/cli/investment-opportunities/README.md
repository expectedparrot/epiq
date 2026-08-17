# Investment opportunities: typed metrics and changing beliefs

This synthetic pipeline compares three companies. Public financing announcements support stage,
amount-raised, and lead-investor claims; private synthetic memos support probabilities and risks.

## Build it

```bash
uv run examples/cli/investment-opportunities/build.sh /tmp/epiq-investments.sqlite
```

The build uses `Quantity[USD]`, `Probability`, `Ref[Investor]`, and a many-valued risk field. All
evidence and claims in [writeback.json](writeback.json) commit together.

## Screen the pipeline

Find opportunities with an internal investment probability of at least 65%:

```bash
uv run epiq --db /tmp/epiq-investments.sqlite query --kind Company \
  --where '{"question":"investment_probability","op":"gte","value":0.65}'
```

Find companies that have raised no more than $10 million:

```bash
uv run epiq --db /tmp/epiq-investments.sqlite query --kind Company \
  --where '{"question":"amount_raised","op":"lte","value":10000000}'
```

Inspect one opportunity or view financing observations chronologically:

```bash
uv run epiq --db /tmp/epiq-investments.sqlite dossier "Aster Labs"
uv run epiq --db /tmp/epiq-investments.sqlite timeline \
  --kind Company --question amount_raised
```

## Record a changed view

Suppose the committee changes Aster's probability after diligence. Add new evidence, assert the
new value, then supersede or retract the old claim after review. Epiq preserves both the valid time
of the assessment and the transaction time at which the database learned it.

```bash
uv run epiq --db /tmp/epiq-investments.sqlite delta
# perform additional evidence and claim writes
uv run epiq --db /tmp/epiq-investments.sqlite delta
```

The second report contains only events since the first report's durable baseline.

## What this exercises

- Unit-bearing quantities and constrained probabilities
- Relationships to separately modeled investors
- Public and private evidence in one project
- Multi-valued risks
- Structured numeric filtering, dossiers, timelines, staleness, and deltas

## Executable documentation check

<!-- epiq-example -->
```bash
examples/cli/investment-opportunities/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Company \
  --where 'investment_probability >= 0.65'
```
