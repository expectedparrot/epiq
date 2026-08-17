# Stress test: corporate ownership and inherited risk

One registry filing supports ownership claims about two companies. `--cell` writes them atomically,
and recursive traversal follows the ownership chain.

```bash
uv run examples/cli/corporate-ownership-risk/build.sh /tmp/epiq-ownership.sqlite

uv run epiq --db /tmp/epiq-ownership.sqlite --actor agent:registry record \
  --source-type web --url "https://example.test/registry/group" \
  --source-title "Corporate registry extract" --retrieved-at 2026-08-17 \
  --locator '{"filing":"2026-1842","section":"Ownership"}' \
  --excerpt "Acorn Devices is owned by Beacon Holdings. Beacon Holdings is owned by Cobalt Group." \
  --valid-from 2026-08-17 \
  --cell "Acorn Devices" parent_company "Beacon Holdings" \
  --cell "Beacon Holdings" parent_company "Cobalt Group"

uv run epiq --db /tmp/epiq-ownership.sqlite --format table related \
  "Acorn Devices" --via parent_company --direction outgoing --depth 5
```

| depth | relationship | from | to |
| ---: | --- | --- | --- |
| 1 | parent_company | Acorn Devices | Beacon Holdings |
| 2 | parent_company | Beacon Holdings | Cobalt Group |

The repeated `record` is idempotent because the fixture already contains the same source and claims.

Remaining gap: traversal finds Cobalt, but cannot express “propagate Cobalt's current high risk to
every descendant” as a declarative rule with derived lineage.

<!-- epiq-example -->
```bash
examples/cli/corporate-ownership-risk/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select count related "Acorn Devices" \
  --via parent_company --direction outgoing --depth 5
```
