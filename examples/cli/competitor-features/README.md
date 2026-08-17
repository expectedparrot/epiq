# Competitor features: an evidence-backed comparison matrix

This synthetic example compares three products across API access, model selection, price,
deployment, and SSO. It is the closest analogue to a conventional spreadsheet, but every cell
retains evidence and valid-time metadata.

## Build and inspect it

```bash
uv run examples/cli/competitor-features/build.sh /tmp/epiq-competitors.sqlite
uv run epiq --db /tmp/epiq-competitors.sqlite matrix --kind Product
uv run epiq --db /tmp/epiq-competitors.sqlite export-xlsx \
  --kind Product --output /tmp/epiq-competitors.xlsx
```

[writeback.json](writeback.json) adds six source fragments and twenty claims atomically.

Find inexpensive products with some API access:

```bash
uv run epiq --db /tmp/epiq-competitors.sqlite query --kind Product \
  --where '{"question":"starting_price","op":"lte","value":300}' \
  --where '{"question":"api_access","op":"ne","value":"none"}'
```

## Correct a category error

The fixture deliberately begins with `sso_support: Bool`. The Beacon documentation says SSO is a
paid add-on, demonstrating that a Boolean loses important meaning. Split or replace the field:

```bash
uv run epiq --db /tmp/epiq-competitors.sqlite evolve-question sso_support \
  --relationship replaces \
  --reason "A Boolean cannot distinguish standard, add-on, and unavailable SSO" \
  --replacement '{
    "name":"sso_availability",
    "value_type":"Enum[standard,paid_addon,enterprise_only,unavailable,unknown]",
    "definition":{"label":"SSO availability"}
  }'

uv run epiq --db /tmp/epiq-competitors.sqlite question-lineage sso_support
uv run epiq --db /tmp/epiq-competitors.sqlite matrix --kind Product
```

The old claims are not coerced into the new enum. They remain attached to the retired Boolean field
until an agent or reviewer researches the successor field.

## Monitor volatile fields

`starting_price` declares a 30-day freshness window:

```bash
uv run epiq --db /tmp/epiq-competitors.sqlite stale --kind Product
uv run epiq --db /tmp/epiq-competitors.sqlite refresh-plan \
  --kind Product --include stale
```

## What this exercises

- Comparison matrices and Excel export
- Enums, Booleans, and monthly price quantities
- Multi-predicate queries
- Volatility and refresh planning
- Executable schema evolution after a category error

## Executable documentation check

<!-- epiq-example -->
```bash
examples/cli/competitor-features/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Product \
  --where 'starting_price <= 300' --where 'api_access != none'
```
