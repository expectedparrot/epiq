# Stress test: normalize procurement quotes

Two supplier quotes are relation rows with compound keys. One report supports cells across both
rows, and landed cost is a persisted derivation rather than an unexplained copied value.

```bash
uv run examples/cli/procurement-normalization/build.sh /tmp/epiq-procurement.sqlite

uv run epiq --db /tmp/epiq-procurement.sqlite materialize \
  --kind Quote --valid-from 2026-08-01

uv run epiq --db /tmp/epiq-procurement.sqlite --format table matrix --kind Quote
```

| Quote | Supplier | Unit price | Shipping | Landed cost |
| --- | --- | ---: | ---: | ---: |
| Atlas Control Board quote | Atlas Supply | 40 | 5 | 45 |
| Beacon Control Board quote | Beacon Parts | 42 | 1 | 43 |

The cheaper sticker price is not the cheaper landed cost. Each derived cell links to both component
claims and the shared report evidence.

The field definition declares `sum(unit_price, shipping_per_unit)` once. `materialize` applies it
to every ready Quote row and reports incomplete rows as `skipped`. Running it after adding future
quotes materializes them without rewriting the formula.

<!-- epiq-example -->
```bash
examples/cli/procurement-normalization/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" materialize --kind Quote --valid-from 2026-08-01
epiq --db "$EPIQ_EXAMPLE_DB" --select rows matrix --kind Quote
```
