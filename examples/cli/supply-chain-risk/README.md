# Tutorial: trace and inherit supply-chain risk

This example asks a deceptively simple question: “Is the Acorn Sensor exposed to a high-risk
supplier?” The answer lives three relationships away. It therefore tests recursive traversal,
mixed relationship types, derived claims, and dependency-aware staleness.

## 1. Build the sourced graph

```bash
examples/cli/supply-chain-risk/build.sh /tmp/epiq-supply-chain.sqlite
epiq use /tmp/epiq-supply-chain.sqlite
epiq --format table matrix --kind Product
epiq --format table matrix --kind Component
```

The fixture records these evidence-backed edges:

| Row kind | Row | Relationship | Value |
| --- | --- | --- | --- |
| Product | Acorn Sensor | `component` | Control Board |
| Component | Control Board | `subcomponent` | Timing Chip |
| Component | Timing Chip | `supplier` | Northstar Semiconductor |

Northstar separately has `risk_level = high`. These are four claims, not one flattened assertion.
That distinction matters when one edge or the supplier rating changes.

## 2. Inspect the path without changing data

```bash
epiq --format table related \
  "Acorn Sensor" --direction outgoing --depth 3
```

| depth | direction | relationship | from | to |
| ---: | --- | --- | --- | --- |
| 1 | outgoing | component | Acorn Sensor | Control Board |
| 2 | outgoing | subcomponent | Control Board | Timing Chip |
| 3 | outgoing | supplier | Timing Chip | Northstar Semiconductor |

No `--via` is supplied because the path deliberately crosses three differently named reference
fields. Use `--via supplier`, for example, when a traversal must follow only one relationship type.

## 3. Turn the graph query into a durable claim

```bash
epiq --actor agent:risk propagate \
  --subject "Acorn Sensor" --direction outgoing --depth 3 \
  --question risk_level --to-question supply_chain_risk --valid-from 2026-08-17

epiq --format table matrix --kind Product
```

The Product row now shows `supply_chain_risk = high`. This is not an unexplained copied cell. Its
derivation has one `operand` dependency—the supplier's risk claim—and three `path` dependencies—the
claims connecting product, board, chip, and supplier. Evidence from all four claims is inherited.

## 4. Ask whether the calculation is still current

```bash
epiq stale-derivations --kind Product
```

The initial result is `count: 0`. If a newer risk rating is asserted or any path edge is superseded,
the command identifies the derived Product claim and the changed dependency. Epiq does not erase or
silently recompute the old result; an agent can inspect the change and deliberately propagate again.

## What this reveals

- Mixed-edge traversal and path provenance now work.
- Propagation chooses the nearest matching source and rejects equally near ambiguity.
- A reusable declarative graph rule is still missing; `propagate` is an explicit materialization.
- Path predicates, graph-wide impact queries, and graph visualization remain future work.

<!-- epiq-example -->
```bash
examples/cli/supply-chain-risk/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq propagate --subject "Acorn Sensor" \
  --direction outgoing --depth 3 --question risk_level \
  --to-question supply_chain_risk --valid-from 2026-08-17
epiq --select count stale-derivations --kind Product
epiq --select rows matrix --kind Product
```
