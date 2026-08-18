# Tutorial: preserve changing multidimensional SaaS prices

`Acorn Cloud → price` is underspecified: price depends on plan, region, billing period, and effective
date. A new price is not necessarily a correction to an old claim. In this example, each dated
combination is an idempotent `relation` row with a compound identity containing all five
dimensions.

## 1. Build and inspect the July catalog

```bash
uv run examples/cli/saas-pricing/build.sh /tmp/epiq-saas-pricing.sqlite
uv run epiq --db /tmp/epiq-saas-pricing.sqlite --format table matrix --kind PriceQuote
```

The fixture initially contains only prices effective July 1:

| Price quote | Product | Plan | Region | Period | Price | Effective |
| --- | --- | --- | --- | --- | ---: | --- |
| Acorn Pro US monthly 2026-07 | Acorn Cloud | Acorn Pro | US | monthly | $110 | 2026-07-01 |
| Acorn Pro EU annual 2026-07 | Acorn Cloud | Acorn Pro | EU | annual | $1,140 | 2026-07-01 |

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite query --kind PriceQuote \
  --where 'region=US' --where 'billing_period=monthly'
```

Output: `matched: 1`, returning the July US monthly quote and its pricing-page evidence.

## 2. Add August as new relation rows

In August, both prices change. Since `effective_date` is part of the fact's identity, do not
supersede the July cells. Create two new rows instead:

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite entity PriceQuote \
  "Acorn Pro US monthly 2026-08" --role relation \
  --identity '{"product":"Acorn Cloud","plan":"Acorn Pro","region":"US","billing_period":"monthly","effective_date":"2026-08-01"}'

uv run epiq --db /tmp/epiq-saas-pricing.sqlite entity PriceQuote \
  "Acorn Pro EU annual 2026-08" --role relation \
  --identity '{"product":"Acorn Cloud","plan":"Acorn Pro","region":"EU","billing_period":"annual","effective_date":"2026-08-01"}'
```

The identity makes ingestion idempotent. Repeating either command resolves to the existing row,
even if a later import supplies a different display name for the same dimensional key.

## 3. Record the changed prices atomically

One August pricing page supports the dimensional cells and prices on both new rows:

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite --actor agent:pricing record \
  --source-type web --url "https://example.test/acorn/pricing/2026-08" \
  --source-title "Acorn regional pricing, August 2026" --retrieved-at 2026-08-17 \
  --excerpt 'Effective August 1, Acorn Pro costs $120 monthly in the US and $1,200 annually in the EU.' \
  --valid-from 2026-08-01 \
  --cell "Acorn Pro US monthly 2026-08" product "Acorn Cloud" \
  --cell "Acorn Pro US monthly 2026-08" plan "Acorn Pro" \
  --cell "Acorn Pro US monthly 2026-08" region US \
  --cell "Acorn Pro US monthly 2026-08" billing_period monthly \
  --cell "Acorn Pro US monthly 2026-08" price_usd 120 \
  --cell "Acorn Pro US monthly 2026-08" effective_date 2026-08-01 \
  --cell "Acorn Pro EU annual 2026-08" product "Acorn Cloud" \
  --cell "Acorn Pro EU annual 2026-08" plan "Acorn Pro" \
  --cell "Acorn Pro EU annual 2026-08" region EU \
  --cell "Acorn Pro EU annual 2026-08" billing_period annual \
  --cell "Acorn Pro EU annual 2026-08" price_usd 1200 \
  --cell "Acorn Pro EU annual 2026-08" effective_date 2026-08-01
```

The evidence and all twelve claims commit atomically. Now the update is visible without erasing the
previous state:

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite --format table matrix --kind PriceQuote
```

| Price quote | Region | Period | Price | Effective |
| --- | --- | --- | ---: | --- |
| Acorn Pro US monthly 2026-07 | US | monthly | $110 | 2026-07-01 |
| Acorn Pro EU annual 2026-07 | EU | annual | $1,140 | 2026-07-01 |
| Acorn Pro US monthly 2026-08 | US | monthly | $120 | 2026-08-01 |
| Acorn Pro EU annual 2026-08 | EU | annual | $1,200 | 2026-08-01 |

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite query --kind PriceQuote \
  --where 'effective_date=2026-08-01'
```

The query reports `matched: 2`. July and August are separate observations of a changing
multidimensional fact—not competing claims in one cell.

## 4. Normalize the new annual quote

Normalize the August EU annual quote to a monthly equivalent as a persisted calculation:

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite derive \
  --subject "Acorn Pro EU annual 2026-08" --question monthly_equivalent_usd \
  --operation linear --parameters '{"scale":0.08333333333333333}' \
  --input-cell "Acorn Pro EU annual 2026-08" price_usd --valid-from 2026-08-01
```

The resulting value is `$100`. Its lineage says `linear`, identifies the August `$1,200` input
claim, and inherits the August pricing-page evidence. This is preferable to pasting an unexplained
normalized value.

## 5. Notice what a naive aggregation means

```bash
uv run epiq --db /tmp/epiq-saas-pricing.sqlite --format table aggregate \
  --kind PriceQuote --question price_usd --op avg --group-by region
```

| group | avg across dated quotes | count |
| --- | ---: | ---: |
| EU | 1,170 | 2 |
| US | 115 | 2 |

Those values are averages across July and August, not “current prices.” This exposes an important
boundary: Epiq can filter a dated relation and aggregate current projections, but it does not yet
offer a built-in `latest by product + plan + region + billing period` projection.

## Product gaps surfaced

- N-ary facts still require relation rows, though compound identities prevent duplicate keys.
- Simple normalization works as a derived claim, but a declarative formula cannot yet branch on
  `billing_period` or look up a dated exchange-rate claim.
- Aggregation can group all current row values, but cannot select the latest row for each
  dimensional key before aggregating or pivoting.

<!-- epiq-example -->
```bash
examples/cli/saas-pricing/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --quiet entity PriceQuote \
  "Acorn Pro US monthly 2026-08" --role relation \
  --identity '{"product":"Acorn Cloud","plan":"Acorn Pro","region":"US","billing_period":"monthly","effective_date":"2026-08-01"}'
epiq --db "$EPIQ_EXAMPLE_DB" --quiet entity PriceQuote \
  "Acorn Pro EU annual 2026-08" --role relation \
  --identity '{"product":"Acorn Cloud","plan":"Acorn Pro","region":"EU","billing_period":"annual","effective_date":"2026-08-01"}'
epiq --db "$EPIQ_EXAMPLE_DB" --actor agent:pricing --quiet record \
  --source-type web --url "https://example.test/acorn/pricing/2026-08" \
  --source-title "Acorn regional pricing, August 2026" --retrieved-at 2026-08-17 \
  --excerpt 'Effective August 1, Acorn Pro costs $120 monthly in the US and $1,200 annually in the EU.' \
  --valid-from 2026-08-01 \
  --cell "Acorn Pro US monthly 2026-08" product "Acorn Cloud" \
  --cell "Acorn Pro US monthly 2026-08" plan "Acorn Pro" \
  --cell "Acorn Pro US monthly 2026-08" region US \
  --cell "Acorn Pro US monthly 2026-08" billing_period monthly \
  --cell "Acorn Pro US monthly 2026-08" price_usd 120 \
  --cell "Acorn Pro US monthly 2026-08" effective_date 2026-08-01 \
  --cell "Acorn Pro EU annual 2026-08" product "Acorn Cloud" \
  --cell "Acorn Pro EU annual 2026-08" plan "Acorn Pro" \
  --cell "Acorn Pro EU annual 2026-08" region EU \
  --cell "Acorn Pro EU annual 2026-08" billing_period annual \
  --cell "Acorn Pro EU annual 2026-08" price_usd 1200 \
  --cell "Acorn Pro EU annual 2026-08" effective_date 2026-08-01
epiq --db "$EPIQ_EXAMPLE_DB" derive --subject "Acorn Pro EU annual 2026-08" \
  --question monthly_equivalent_usd --operation linear \
  --parameters '{"scale":0.08333333333333333}' \
  --input-cell "Acorn Pro EU annual 2026-08" price_usd --valid-from 2026-08-01
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind PriceQuote \
  --where 'effective_date=2026-08-01'
```
