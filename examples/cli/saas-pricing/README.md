# Stress test: multidimensional SaaS pricing

`Acorn Cloud → price` is underspecified: price depends on plan, region, billing period, and effective
date. Each combination is now an idempotent `relation` row whose compound identity contains those
five dimensions; its display name can change without changing relational identity.

```bash
uv run examples/cli/saas-pricing/build.sh /tmp/epiq-saas-pricing.sqlite
uv run epiq --db /tmp/epiq-saas-pricing.sqlite --format table matrix --kind PriceQuote
```

| Price quote | Product | Plan | Region | Period | Price | Effective |
| --- | --- | --- | --- | --- | ---: | --- |
| Acorn Pro US monthly 2026-08 | Acorn Cloud | Acorn Pro | US | monthly | $120 | 2026-08-01 |
| Acorn Pro EU annual 2026-08 | Acorn Cloud | Acorn Pro | EU | annual | $1,200 | 2026-08-01 |

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite query --kind PriceQuote --where 'region=US' --where 'billing_period=monthly'
```

Output: `matched: 1`, returning the US monthly quote with its pricing-page evidence.

One source can now update cells across both quote rows without JSON or evidence-ID plumbing:

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite --actor agent:pricing record \
  --source-type web --url "https://example.test/acorn/pricing" \
  --source-title "Acorn regional pricing" --retrieved-at 2026-08-17 \
  --excerpt "Acorn Pro is $120 monthly in the US and $1,200 annually in the EU, effective August 1, 2026." \
  --valid-from 2026-08-01 \
  --cell "Acorn Pro US monthly 2026-08" price_usd 120 \
  --cell "Acorn Pro EU annual 2026-08" price_usd 1200
```

The evidence and both claims commit atomically. Because the fixture already contains those exact
claims, rerunning this command returns their existing IDs rather than duplicating them.

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite --format table aggregate \
  --kind PriceQuote --question price_usd --op avg --group-by region
```

| group | avg | count |
| --- | ---: | ---: |
| EU | 1,200 | 1 |
| US | 120 | 1 |

## Product gaps surfaced

- N-ary facts still require relation rows, but compound identities now prevent duplicate keys.
- Currency conversion and normalized monthly cost require computed fields.
- Aggregation can now group current values, but cannot pivot or select the latest dimensional key.

<!-- epiq-example -->
```bash
examples/cli/saas-pricing/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind PriceQuote --where 'region=US'
```
