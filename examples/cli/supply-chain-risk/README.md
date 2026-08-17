# Stress test: recursive supply-chain risk

The Acorn Sensor directly contains a Control Board, which contains a Timing Chip, which is supplied
by a high-risk supplier. Epiq stores each sourced edge, but current traversal is one hop at a time.

```bash
uv run examples/cli/supply-chain-risk/build.sh /tmp/epiq-supply-chain.sqlite
uv run epiq --db /tmp/epiq-supply-chain.sqlite matrix --kind Product
uv run epiq --db /tmp/epiq-supply-chain.sqlite matrix --kind Component
```

| Product | Direct component |
| --- | --- |
| Acorn Sensor | Control Board |

| Component | Subcomponent | Supplier |
| --- | --- | --- |
| Control Board | Timing Chip | Unasked |
| Timing Chip | Unasked | Northstar Semiconductor |

```bash
uv run epiq --db /tmp/epiq-supply-chain.sqlite related "Acorn Sensor" --direction outgoing
uv run epiq --db /tmp/epiq-supply-chain.sqlite related "Control Board" --direction outgoing
uv run epiq --db /tmp/epiq-supply-chain.sqlite related "Timing Chip" --direction outgoing
```

Each command returns one edge. A human can follow the three steps to the supplier, but Epiq cannot
yet answer “which products transitively depend on a high-risk supplier?” in one query.

## Product gaps surfaced

- `related` lacks recursive traversal, path return, depth limits, and cycle handling.
- There is no rule or computed claim propagating supplier risk to components and products.
- Product-to-component and component-to-component edges require different fields.
- Graph-wide impact queries and visualization are absent.
- Cardinality constraints exist, but referential deletion and dependency policies remain limited.

<!-- epiq-example -->
```bash
examples/cli/supply-chain-risk/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select count related "Control Board" --direction outgoing
```
