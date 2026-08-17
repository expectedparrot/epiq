# Stress test: recursive supply-chain risk

The Acorn Sensor directly contains a Control Board, which contains a Timing Chip, which is supplied
by a high-risk supplier. Epiq stores each sourced edge, but current traversal is one hop at a time.

```bash
uv run examples/cli/supply-chain-risk/build.sh /tmp/epiq-supply-chain.sqlite
uv run epiq --db /tmp/epiq-supply-chain.sqlite --format table matrix --kind Product
uv run epiq --db /tmp/epiq-supply-chain.sqlite --format table matrix --kind Component
```

| Product | Direct component |
| --- | --- |
| Acorn Sensor | Control Board |

| Component | Subcomponent | Supplier |
| --- | --- | --- |
| Control Board | Timing Chip | Unasked |
| Timing Chip | Unasked | Northstar Semiconductor |

```bash
uv run epiq --db /tmp/epiq-supply-chain.sqlite --format table related \
  "Acorn Sensor" --direction outgoing --depth 3
```

Output:

| depth | direction | relationship | from | to |
| ---: | --- | --- | --- | --- |
| 1 | outgoing | component | Acorn Sensor | Control Board |
| 2 | outgoing | subcomponent | Control Board | Timing Chip |
| 3 | outgoing | supplier | Timing Chip | Northstar Semiconductor |

Epiq can now return the dependency path in one bounded traversal. It still cannot filter the path
by the supplier's `risk_level` or automatically propagate that risk back to the product.

## Product gaps surfaced

- Recursive traversal now has a depth limit and cycle protection, but not path-level predicates.
- There is no rule or computed claim propagating supplier risk to components and products.
- Product-to-component and component-to-component edges require different fields.
- Graph-wide impact queries and visualization are absent.
- Cardinality constraints exist, but referential deletion and dependency policies remain limited.

<!-- epiq-example -->
```bash
examples/cli/supply-chain-risk/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select count related "Control Board" --direction outgoing
```
