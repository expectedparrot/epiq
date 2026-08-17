# Stress test: normalize procurement quotes

Two supplier quotes are relation rows with compound keys. One report supports cells across both
rows, and landed cost is a persisted derivation rather than an unexplained copied value.

```bash
uv run examples/cli/procurement-normalization/build.sh /tmp/epiq-procurement.sqlite

uv run epiq --db /tmp/epiq-procurement.sqlite derive \
  --subject "Atlas Control Board quote" --question landed_unit_cost --operation sum \
  --valid-from 2026-08-01 --input-cell "Atlas Control Board quote" unit_price \
  --input-cell "Atlas Control Board quote" shipping_per_unit

uv run epiq --db /tmp/epiq-procurement.sqlite derive \
  --subject "Beacon Control Board quote" --question landed_unit_cost --operation sum \
  --valid-from 2026-08-01 --input-cell "Beacon Control Board quote" unit_price \
  --input-cell "Beacon Control Board quote" shipping_per_unit

uv run epiq --db /tmp/epiq-procurement.sqlite --format table matrix --kind Quote
```

| Quote | Supplier | Unit price | Shipping | Landed cost |
| --- | --- | ---: | ---: | ---: |
| Atlas Control Board quote | Atlas Supply | 40 | 5 | 45 |
| Beacon Control Board quote | Beacon Parts | 42 | 1 | 43 |

The cheaper sticker price is not the cheaper landed cost. Each derived cell links to both component
claims and the shared report evidence.

Remaining gap: formulas are invoked imperatively; Epiq does not yet declare a formula once and
automatically materialize it for every existing and future Quote row.

<!-- epiq-example -->
```bash
examples/cli/procurement-normalization/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" derive --subject "Atlas Control Board quote" \
  --question landed_unit_cost --operation sum --valid-from 2026-08-01 \
  --input-cell "Atlas Control Board quote" unit_price \
  --input-cell "Atlas Control Board quote" shipping_per_unit
epiq --db "$EPIQ_EXAMPLE_DB" --select rows matrix --kind Quote
```
