# Tutorial: build a derived regional sales table

This tutorial distinguishes two forms of derived data:

1. `net_revenue` is a **persisted derived claim** on every order, with evidence and dependency
   lineage.
2. The regional rollup is a **read-only derived table** computed from the current order projection.

Build the sourced order ledger:

```bash
uv run examples/cli/regional-sales-rollup/build.sh /tmp/epiq-regional-sales-rollup.sqlite
uv run epiq use /tmp/epiq-regional-sales-rollup.sqlite

uv run epiq --format table matrix \
  --kind Order --questions region,units,unit_price,discount_rate
```

The schema declares net revenue once as `=B1*C1*(1-D1)`, normalized to
`x0*x1*(1-x2)`. Materialize it across the table:

```bash
uv run epiq materialize \
  --kind Order --valid-from 2026-08-18

uv run epiq --format table matrix \
  --kind Order --questions region,units,unit_price,discount_rate,net_revenue
```

| Order | Region | Units | Unit price | Discount | Net revenue |
| --- | --- | ---: | ---: | ---: | ---: |
| Order 1001 | North | 10 | 100 | 0.00 | 1,000 |
| Order 1002 | North | 5 | 200 | 0.10 | 900 |
| Order 1003 | South | 20 | 50 | 0.05 | 950 |
| Order 1004 | South | 4 | 300 | 0.25 | 900 |

Now derive a summary table without creating artificial “North total” entities or copying totals
into claims:

```bash
uv run epiq --format table aggregate \
  --kind Order --question net_revenue --op sum --group-by region
```

| region | count | sum |
| --- | ---: | ---: |
| North | 2 | 1,900 |
| South | 2 | 1,850 |

The rollup is always a projection of current claims. It is appropriate for reports and exports,
whereas `net_revenue` is persisted because users may need to inspect exactly which inputs and
evidence produced each order-level result.

You can derive other views without changing the schema:

```bash
epiq --format table aggregate \
  --kind Order --question net_revenue --op avg --group-by region

epiq query --kind Order \
  --where 'net_revenue >= 950'
```

<!-- epiq-example -->
```bash
examples/cli/regional-sales-rollup/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq materialize --kind Order --valid-from 2026-08-18
epiq --select groups aggregate --kind Order \
  --question net_revenue --op sum --group-by region
epiq --select rows matrix --kind Order
```
