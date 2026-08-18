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
  --definition '{"label":"API access","cardinality":"one","volatility":"dynamic","freshness_days":90}'

epiq question starting_price --for Product \
  --type 'Quantity[USD/month]' \
  --definition '{"label":"Starting monthly price","cardinality":"one","volatility":"dynamic","freshness_days":30}'
```

The type prevents an agent from writing `maybe` into `api_access` or `cheap` into
`starting_price`. The freshness policy says price should be revisited after 30 days.

At this point the matrix has two rows and two columns, but its cells are `Unasked`:

```bash
epiq matrix --kind Product
```

The CLI returns JSON containing the schema and rows. Rendered as a table, its result is:

| Product | API access (`Enum`) | Starting monthly price (`Quantity[USD/month]`) |
| --- | --- | ---: |
| Acorn Interview | Unasked | Unasked |
| Beacon Research | Unasked | Unasked |

The entities became rows and the questions became columns. `Unasked` means no answer or completed
unsuccessful search has yet been recorded; it does not mean `none` or zero.

## 4. Record evidence and the answers it supports

Epiq does not fetch this URL. A human or research agent has already read the page and submits the
specific passage it relied upon. Because this passage supports two cells, record them together:

```bash
epiq --actor agent:market-research record \
  --subject "Acorn Interview" \
  --source-type web \
  --url "https://example.test/acorn/pricing" \
  --source-title "Acorn Interview pricing" \
  --retrieved-at 2026-07-01 \
  --excerpt "The Starter plan costs $249 per month and includes limited API access." \
  --valid-from 2026-07-01 \
  --answer starting_price 249 \
  --answer api_access limited
```

The response shows exactly what was written (IDs vary by project):

```json
{
  "answer_count": 2,
  "claim_ids": ["clm_...", "clm_..."],
  "evidence_id": "evd_...",
  "ok": true,
  "source_id": "src_..."
}
```

Internally, evidence remains separate from
the answers: the source passage is stored once and linked to both independently typed claims. The
operation is atomic, so a misspelled enum or invalid price would cause all three records to roll
back.

Add Beacon using the same pattern:

```bash
epiq --actor agent:market-research record \
  --subject "Beacon Research" \
  --source-type web \
  --url "https://example.test/beacon/pricing" \
  --source-title "Beacon Research plans" \
  --retrieved-at 2026-07-01 \
  --excerpt "Beacon Pro costs $599 per month and provides full API access." \
  --valid-from 2026-07-01 \
  --answer starting_price 599 \
  --answer api_access full
```

`--valid-from` is when the fact was true. The event timestamp separately records when Epiq learned
it. `--actor` records who performed the interpretation.

## 5. Inspect the populated table and its provenance

```bash
epiq matrix --kind Product
epiq dossier "Acorn Interview"
```

The current projection is now:

| Product | API access | Starting monthly price |
| --- | --- | ---: |
| Acorn Interview | limited | $249/month |
| Beacon Research | full | $599/month |

The matrix is convenient, but it is not the whole database. The dossier shows that Acorn's two
values came from the same excerpt, along with the source URL, retrieval date, observation date,
confidence, actor, claim IDs, and evidence ID.

An abridged rendering of the dossier lineage is:

| Field | Value | Confidence | As of | Actor | Evidence |
| --- | --- | --- | --- | --- | --- |
| API access | limited | high | 2026-07-01 | `agent:market-research` | Acorn Interview pricing |
| Starting monthly price | 249 | high | 2026-07-01 | `agent:market-research` | Acorn Interview pricing |

## 6. Ask a database question

```bash
epiq query --kind Product \
  --where 'starting_price <= 300' --where 'api_access != none'
```

The response includes the parsed predicates and the one matching row. Abridged:

```json
{
  "entity_kind": "Product",
  "query": {
    "matched": 1,
    "predicates": [
      {"question": "starting_price", "op": "lte", "value": 300},
      {"question": "api_access", "op": "ne", "value": "none"}
    ]
  },
  "rows": [{"name": "Acorn Interview"}]
}
```

Acorn satisfies both predicates, while Beacon's price exceeds 300. The query operates only on
current supported claims. It does not scrape missing cells or infer answers.

## 7. Notice when the schema is wrong

A field such as `has_sso: Bool` often hides a category error. First, create and populate the flawed
field from a source that says SSO is available as an add-on:

```bash
epiq question has_sso --for Product --type Bool \
  --definition '{"label":"Has SSO"}'

epiq --actor agent:market-research record \
  --subject "Beacon Research" \
  --source-type web \
  --url "https://example.test/beacon/security" \
  --source-title "Beacon Research security options" \
  --retrieved-at 2026-08-17 \
  --excerpt "Single sign-on is available as a paid enterprise add-on." \
  --valid-from 2026-08-17 \
  --question has_sso \
  --value true
```

The table can display only `true`. It loses the commercially important fact that SSO costs extra.
Replace the field with a vocabulary that can express the actual alternatives:

```bash

epiq evolve-question has_sso \
  --relationship replaces \
  --reason "Boolean cannot distinguish how SSO is offered" \
  --replacement '{"name":"sso_availability","value_type":"Enum[standard,paid_addon,enterprise_only,unavailable,unknown]","definition":{"label":"SSO availability"}}'

epiq question-lineage has_sso
```

Output:

```json
{
  "name": "has_sso",
  "predecessors": [],
  "question_id": "q_has_sso_v1",
  "successors": [
    {
      "name": "sso_availability",
      "question_id": "q_sso_availability_v1",
      "reason": "Boolean cannot distinguish how SSO is offered",
      "relationship": "replaces"
    }
  ]
}
```

Old Boolean claims remain auditable but are not silently coerced. Research the successor field—the
same source passage is deduplicated automatically:

```bash
epiq --actor agent:market-research record \
  --subject "Beacon Research" \
  --source-type web \
  --url "https://example.test/beacon/security" \
  --source-title "Beacon Research security options" \
  --retrieved-at 2026-08-17 \
  --excerpt "Single sign-on is available as a paid enterprise add-on." \
  --valid-from 2026-08-17 \
  --question sso_availability \
  --value paid_addon

epiq matrix --kind Product
```

The active matrix output, rendered as a table, is now:

| Product | API access | Starting monthly price | SSO availability |
| --- | --- | ---: | --- |
| Acorn Interview | limited | $249/month | Unasked |
| Beacon Research | full | $599/month | paid_addon |

The question lineage still connects that improved field to the retired Boolean and its historical
claim.

## 8. Identify facts that need refreshing

Starting price was declared dynamic with a 30-day freshness window. As observation dates age, use:

```bash
epiq stale --kind Product
epiq refresh-plan --kind Product --include stale
```

Because the price observations in this tutorial are from July 1, `stale` reports both price cells
after the 30-day window. Abridged output:

```json
{
  "count": 2,
  "entity_kind": "Product",
  "cells": [
    {"entity_name": "Acorn Interview", "question": "starting_price", "state": "Answered"},
    {"entity_name": "Beacon Research", "question": "starting_price", "state": "Answered"}
  ]
}
```

`refresh-plan` turns those cells into tasks rather than doing research itself:

```json
{
  "count": 2,
  "tasks": [
    {
      "entity_name": "Acorn Interview",
      "question": "starting_price",
      "reasons": ["stale"],
      "suggested_query": "\"Acorn Interview\" Starting monthly price",
      "existing_source_urls": ["https://example.test/acorn/pricing"]
    },
    {
      "entity_name": "Beacon Research",
      "question": "starting_price",
      "reasons": ["stale"],
      "suggested_query": "\"Beacon Research\" Starting monthly price",
      "existing_source_urls": ["https://example.test/beacon/pricing"]
    }
  ]
}
```

An external agent can execute those tasks and write new observations back with `record`; Epiq
itself remains deterministic and does not browse or call a model.

## Finished fixture and next experiments

The packaged builder creates three products and five populated fields:

```bash
examples/cli/competitor-features/build.sh /tmp/epiq-competitors.sqlite
epiq use /tmp/epiq-competitors.sqlite
epiq matrix --kind Product
epiq stale --kind Product
epiq export-xlsx --kind Product \
  --output /tmp/epiq-competitors.xlsx
```

Read [schema.json](schema.json) only after following the CLI steps above. It is the declarative
equivalent of creating the project, rows, and columns. [writeback.json](writeback.json) is an atomic
import of evidence and claims—the form a research agent commonly emits in production.

<!-- epiq-example -->
```bash
examples/cli/competitor-features/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq --select query.matched query --kind Product \
  --where 'starting_price <= 300' --where 'api_access != none'
```
