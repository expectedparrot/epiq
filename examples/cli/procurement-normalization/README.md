# Tutorial: normalize procurement quotes with a table formula

Two supplier quotes are relation rows with compound keys. One report supports cells across both
rows, and landed cost is a persisted derivation rather than an unexplained copied value.

The target field carries this declarative definition:

```json
{"formula":{"operation":"sum","inputs":["unit_price","shipping_per_unit"]}}
```

```bash
examples/cli/procurement-normalization/build.sh /tmp/epiq-procurement.sqlite
epiq use /tmp/epiq-procurement.sqlite

epiq materialize \
  --kind Quote --valid-from 2026-08-01

epiq --format table matrix --kind Quote
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

```bash
epiq stale-derivations --kind Quote
```

The result is initially zero. Replacing either component price marks only the affected quote's
landed-cost claim stale.

<!-- epiq-example -->
```bash
examples/cli/procurement-normalization/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq materialize --kind Quote --valid-from 2026-08-01
epiq --select count stale-derivations --kind Quote
epiq --select rows matrix --kind Quote
```
