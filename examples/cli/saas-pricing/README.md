# Stress test: multidimensional SaaS pricing

`Acorn Cloud → price` is underspecified: price depends on plan, region, billing period, and effective
date. The current workaround makes each combination a `PriceQuote` row.

```bash
uv run examples/cli/saas-pricing/build.sh /tmp/epiq-saas-pricing.sqlite
uv run epiq --db /tmp/epiq-saas-pricing.sqlite matrix --kind PriceQuote
```

| Price quote | Product | Plan | Region | Period | Price | Effective |
| --- | --- | --- | --- | --- | ---: | --- |
| Acorn Pro US monthly 2026-08 | Acorn Cloud | Acorn Pro | US | monthly | $120 | 2026-08-01 |
| Acorn Pro EU annual 2026-08 | Acorn Cloud | Acorn Pro | EU | annual | $1,200 | 2026-08-01 |

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite query --kind PriceQuote --where 'region=US' --where 'billing_period=monthly'
```

Output: `matched: 1`, returning the US monthly quote with its pricing-page evidence.

## Product gaps surfaced

- N-ary facts require synthetic join entities and verbose names.
- There are no compound uniqueness constraints over product, plan, region, period, and date.
- Currency conversion and normalized monthly cost require computed fields.
- Queries cannot group, pivot, or select the latest quote for each dimensional key.

<!-- epiq-example -->
```bash
examples/cli/saas-pricing/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind PriceQuote --where 'region=US'
```
