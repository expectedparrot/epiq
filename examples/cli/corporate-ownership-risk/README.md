# Tutorial: propagate risk through corporate ownership

One registry filing supports ownership claims about two companies. `--cell` writes them atomically,
and recursive traversal follows the ownership chain.

```bash
uv run examples/cli/corporate-ownership-risk/build.sh /tmp/epiq-ownership.sqlite
uv run epiq use /tmp/epiq-ownership.sqlite

uv run epiq --actor agent:registry record \
  --source-type web --url "https://example.test/registry/group" \
  --source-title "Corporate registry extract" --retrieved-at 2026-08-17 \
  --locator '{"filing":"2026-1842","section":"Ownership"}' \
  --excerpt "Acorn Devices is owned by Beacon Holdings. Beacon Holdings is owned by Cobalt Group." \
  --valid-from 2026-08-17 \
  --cell "Acorn Devices" parent_company "Beacon Holdings" \
  --cell "Beacon Holdings" parent_company "Cobalt Group"

uv run epiq --format table related \
  "Acorn Devices" --via parent_company --direction outgoing --depth 5
```

| depth | relationship | from | to |
| ---: | --- | --- | --- |
| 1 | parent_company | Acorn Devices | Beacon Holdings |
| 2 | parent_company | Beacon Holdings | Cobalt Group |

Materialize the nearest related owner's risk on Acorn, retaining Cobalt's risk claim plus both
ownership claims and their evidence as typed dependencies:

```bash
uv run epiq propagate \
  --subject "Acorn Devices" --via parent_company --direction outgoing --depth 5 \
  --question risk_level --to-question inherited_risk --valid-from 2026-08-17

uv run epiq --format table matrix --kind Company
```

The repeated `record` is idempotent because the fixture already contains the same source and claims.
Propagation refuses to choose when multiple source claims are equally near.

```bash
uv run epiq stale-derivations --kind Company
```

This initially returns zero. A newer Cobalt risk claim, or a retracted/superseded ownership edge,
makes Acorn's derived claim stale without erasing the historical calculation.

<!-- epiq-example -->
```bash
examples/cli/corporate-ownership-risk/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq propagate --subject "Acorn Devices" \
  --via parent_company --direction outgoing --depth 5 --question risk_level \
  --to-question inherited_risk --valid-from 2026-08-17
epiq --select count stale-derivations --kind Company
epiq --select rows matrix --kind Company
```
