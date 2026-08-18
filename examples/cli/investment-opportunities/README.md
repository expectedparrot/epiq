# Tutorial: research investment opportunities and revise a belief

An investment table mixes observable facts—funding and lead investor—with judgments such as risk
and investment probability. Epiq can store both, provided each answer says where it came from and
who made the interpretation.

All companies and assessments below are synthetic.

## 1. Model companies and investors as different kinds of entity

```bash
epiq use /tmp/investments.sqlite
epiq init --name "Investment pipeline tutorial"
epiq entity Company "Aster Labs"
epiq entity Investor "Northstar Ventures"
```

`epiq use` selects the database for subsequent commands in this workspace. Run `epiq db` whenever
you want to confirm the active project.

Why not store the investor as plain text? Because a reference preserves identity. `Northstar`, an
alias, and its durable entity ID can all resolve to the same investor, and the investor can later
have its own fields.

## 2. Define facts, judgments, and a relationship

```bash
epiq question amount_raised --for Company \
  --type 'Quantity[USD]' \
  --definition '{"label":"Total disclosed funding","cardinality":"one","freshness_days":90}'

epiq question lead_investor --for Company \
  --type 'Ref[Investor]' \
  --definition '{"label":"Lead investor","cardinality":"one"}'

epiq question investment_probability --for Company \
  --type Probability \
  --definition '{"label":"Current probability of investing","cardinality":"one"}'

epiq question key_risk --for Company \
  --type String \
  --definition '{"label":"Key risk","cardinality":"many"}'
```

`Probability` accepts only values from 0 through 1. `key_risk` is many-valued because two distinct
risks can both be supported; they should not be treated as contradictory cell values.

The initial projection is:

```bash
epiq matrix --kind Company
```

| Company | Total disclosed funding | Lead investor | Investment probability | Key risk |
| --- | ---: | --- | ---: | --- |
| Aster Labs | Unasked | Unasked | Unasked | Unasked |

## 3. Record a public fact

```bash
epiq --actor agent:diligence record \
  --subject "Aster Labs" \
  --source-type web \
  --url "https://example.test/aster/series-a" \
  --source-title "Aster Labs announces Series A" \
  --retrieved-at 2026-08-17 \
  --excerpt "Aster Labs has raised $8 million in a round led by Northstar Ventures." \
  --valid-from 2026-06-10 \
  --answer amount_raised 8000000 \
  --answer lead_investor "Northstar Ventures"
```

Output (IDs vary):

```json
{
  "answer_count": 2,
  "claim_ids": ["clm_...", "clm_..."],
  "evidence_id": "evd_...",
  "ok": true,
  "source_id": "src_..."
}
```

The reference value is entered by name but stored as the investor's stable entity ID. Run the
dossier to see both the stored value and its human-readable decoration:

```bash
epiq dossier "Aster Labs"
```

The dossier's relevant lineage renders as:

| Field | Value | Actor | Evidence |
| --- | --- | --- | --- |
| Total disclosed funding | 8,000,000 USD | `agent:diligence` | Aster Labs announces Series A |
| Lead investor | Northstar Ventures | `agent:diligence` | Aster Labs announces Series A |

## 4. Record an internal judgment without inventing a URL

Evidence may be an interview, personal knowledge, a report, or model output:

```bash
epiq --actor partner:maya record \
  --subject "Aster Labs" \
  --source-type report \
  --source-title "Aster diligence memo, v1" \
  --retrieved-at 2026-08-17 \
  --excerpt "The team assigns a 0.65 probability of investing. Main risk: customer concentration." \
  --valid-from 2026-08-17 \
  --confidence medium \
  --answer investment_probability 0.65 \
  --answer key_risk "Customer concentration"
```

The evidence has a source type and durable internal locator, but no fake web address.

```bash
epiq matrix --kind Company
```

| Company | Total disclosed funding | Lead investor | Investment probability | Key risk |
| --- | ---: | --- | ---: | --- |
| Aster Labs | 8,000,000 USD | Northstar Ventures | 0.65 | Customer concentration |

## 5. Query the current projection

```bash
epiq query --kind Company \
  --where 'investment_probability >= 0.60' --where 'amount_raised <= 10000000'
```

Abridged output:

```json
{
  "entity_kind": "Company",
  "query": {"matched": 1},
  "rows": [{"name": "Aster Labs"}]
}
```

## 6. Preserve a changed belief

Suppose a second memo raises the probability after customer interviews:

```bash
epiq --actor partner:maya record \
  --subject "Aster Labs" \
  --source-type report \
  --source-title "Aster diligence memo, v2" \
  --retrieved-at 2026-08-24 \
  --excerpt "After customer calls, the team raises its investment probability to 0.80." \
  --valid-from 2026-08-24 \
  --confidence medium \
  --question investment_probability \
  --value 0.80

epiq contradictions --kind Company
```

Until a reviewer says that v2 replaces v1, the single-valued field is `Contested`:

```json
{
  "count": 1,
  "cells": [
    {
      "entity_name": "Aster Labs",
      "question": "investment_probability",
      "values": [0.65, 0.8]
    }
  ]
}
```

Use `epiq dossier "Aster Labs"` to obtain the exact claim IDs, then `supersede` or `retract` the v1
claim after review. Epiq does not guess that newer means replacement: the two records might instead
be rival assessments. Both transaction time and valid time remain in history.

## Finished fixture

The full example contains three companies, multiple risks, and public and private evidence:

```bash
examples/cli/investment-opportunities/build.sh /tmp/epiq-investments.sqlite
epiq use /tmp/epiq-investments.sqlite
epiq matrix --kind Company
epiq timeline --kind Company \
  --question amount_raised
```

[schema.json](schema.json) is the repeatable declaration; [writeback.json](writeback.json) is the
atomic agent writeback. They encode the same concepts as the commands above.

<!-- epiq-example -->
```bash
examples/cli/investment-opportunities/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq --select query.matched query --kind Company \
  --where 'investment_probability >= 0.65'
```
