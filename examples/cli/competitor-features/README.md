# Tutorial: build a competitor comparison one fact at a time

Suppose you want a spreadsheet with one product per row and fields for API access, deployment, and
starting price. Epiq can render that matrix, but it first records what every cell means and why you
believe it.

This tutorial uses synthetic companies and sources so it can be run safely offline.

## 1. Select a database and create an empty project

Select the database once for this workspace. Later commands automatically use it:

```bash
epiq use /tmp/competitors.sqlite
epiq init --name "Competitor tutorial"
```

`epiq use` writes the selection to `.epiq/config.json`; `epiq db` shows the current selection. An
Epiq project is one SQLite file. `init` creates its event ledger and materialized tables.

## 2. Add rows

Products are the things being compared, so they are entities of kind `Product`:

```bash
epiq entity Product "Acorn Interview"
epiq entity Product "Beacon Research"
```

The returned `entity_id` is durable. Later commands may use either that ID or the exact name.

## 3. Add columns

In Epiq, a column is a typed question about an entity kind:

```bash
epiq question api_access --for Product \
  --type 'Enum[none,limited,full]' \
  --definition '{"label":"API access","cardinality":"one","volatility":"medium"}'

epiq question starting_price --for Product \
  --type 'Quantity[USD/month]' \
  --definition '{"label":"Starting monthly price","cardinality":"one","freshness_days":30}'
```

The type prevents an agent from writing `maybe` into `api_access` or `cheap` into
`starting_price`. The freshness policy says price should be revisited after 30 days.

At this point the matrix has two rows and two columns, but its cells are `Unasked`:

```bash
epiq matrix --kind Product
```

## 4. Store a source passage

Epiq does not fetch this URL. A human or research agent has already read the page and submits the
specific passage it relied upon:

```bash
PRICE_EVIDENCE=$(epiq --actor agent:market-research evidence \
  --url 'https://example.test/acorn/pricing' \
  --title 'Acorn Interview pricing' \
  --retrieved-at 2026-08-17 \
  --excerpt 'The Starter plan costs $249 per month and includes limited API access.' \
  | jq -r .evidence_id)
```

The shell variable contains the evidence ID returned by Epiq. Evidence is separate from an answer
because one passage can support several claims, and several passages can support one claim.

## 5. Turn the passage into typed answers

```bash
epiq --actor agent:market-research assert \
  --subject "Acorn Interview" --question starting_price --value 249 \
  --valid-from 2026-08-17 --evidence "$PRICE_EVIDENCE" --confidence high

epiq --actor agent:market-research assert \
  --subject "Acorn Interview" --question api_access --value limited \
  --valid-from 2026-08-17 --evidence "$PRICE_EVIDENCE" --confidence high
```

`--valid-from` is when the fact was true. The event timestamp separately records when Epiq learned
it. `--actor` records who performed the interpretation.

Now inspect both the spreadsheet-like view and the record behind one row:

```bash
epiq matrix --kind Product
epiq dossier "Acorn Interview"
```

The matrix is convenient; the dossier teaches you what Epiq actually stored: the typed value,
confidence, observation date, evidence excerpt, source, and actor.

## 6. Ask a database question

```bash
epiq query --kind Product \
  --where 'starting_price <= 300' --where 'api_access != none'
```

The query operates on current supported claims. It does not scrape missing cells or infer answers.

## 7. Notice when the schema is wrong

A field such as `has_sso: Bool` often hides a category error: SSO might be standard, a paid add-on,
enterprise-only, or unavailable. Epiq versions schema changes instead of rewriting history:

```bash
epiq question has_sso --for Product --type Bool \
  --definition '{"label":"Has SSO"}'

epiq evolve-question has_sso \
  --relationship replaces \
  --reason "Boolean cannot distinguish how SSO is offered" \
  --replacement '{"name":"sso_availability","value_type":"Enum[standard,paid_addon,enterprise_only,unavailable,unknown]","definition":{"label":"SSO availability"}}'

epiq question-lineage has_sso
```

Old Boolean claims remain auditable. They are not silently coerced into the new enum.

## Finished fixture and next experiments

The packaged builder creates three products and five populated fields:

```bash
uv run examples/cli/competitor-features/build.sh /tmp/epiq-competitors.sqlite
uv run epiq --db /tmp/epiq-competitors.sqlite matrix --kind Product
uv run epiq --db /tmp/epiq-competitors.sqlite stale --kind Product
uv run epiq --db /tmp/epiq-competitors.sqlite export-xlsx --kind Product \
  --output /tmp/epiq-competitors.xlsx
```

Read [schema.json](schema.json) only after following the CLI steps above. It is the declarative
equivalent of creating the project, rows, and columns. [writeback.json](writeback.json) is an atomic
import of evidence and claims—the form a research agent commonly emits in production.

<!-- epiq-example -->
```bash
examples/cli/competitor-features/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Product \
  --where 'starting_price <= 300' --where 'api_access != none'
```
