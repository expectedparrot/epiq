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

## 3. Record a public fact

```bash
FUNDING_EVIDENCE=$(epiq --actor agent:diligence evidence \
  --url 'https://example.test/aster/series-a' \
  --title 'Aster Labs announces Series A' \
  --retrieved-at 2026-08-17 \
  --excerpt 'Aster Labs has raised $8 million in a round led by Northstar Ventures.' \
  | jq -r .evidence_id)

epiq --actor agent:diligence assert \
  --subject "Aster Labs" --question amount_raised --value 8000000 \
  --valid-from 2026-06-10 --evidence "$FUNDING_EVIDENCE" --confidence high

epiq --actor agent:diligence assert \
  --subject "Aster Labs" --question lead_investor --value "Northstar Ventures" \
  --valid-from 2026-06-10 --evidence "$FUNDING_EVIDENCE" --confidence high
```

The reference value is entered by name but stored as the investor's stable entity ID. Run the
dossier to see both the stored value and its human-readable decoration:

```bash
epiq dossier "Aster Labs"
```

## 4. Record an internal judgment without inventing a URL

Evidence may be an interview, personal knowledge, a report, or model output:

```bash
MEMO_EVIDENCE=$(epiq --actor partner:maya evidence \
  --type report --title 'Aster diligence memo, v1' --retrieved-at 2026-08-17 \
  --excerpt 'The team assigns a 0.65 probability of investing. Main risk: customer concentration.' \
  | jq -r .evidence_id)

epiq --actor partner:maya assert \
  --subject "Aster Labs" --question investment_probability --value 0.65 \
  --valid-from 2026-08-17 --evidence "$MEMO_EVIDENCE" --confidence medium

epiq --actor partner:maya assert \
  --subject "Aster Labs" --question key_risk --value 'Customer concentration' \
  --valid-from 2026-08-17 --evidence "$MEMO_EVIDENCE" --confidence medium
```

The evidence has a source type and durable internal locator, but no fake web address.

## 5. Query the current projection

```bash
epiq query --kind Company \
  --where 'investment_probability >= 0.60' --where 'amount_raised <= 10000000'
```

## 6. Preserve a changed belief

If diligence changes the probability, add the new memo and assertion. Then explicitly supersede
or retract the old claim after review. Epiq does not overwrite the old judgment: transaction time
records when each version entered the database, while valid time records when it applied.

Use `dossier "Aster Labs"` to find the existing claim ID, then inspect the correction commands:

```bash
epiq supersede --help
epiq retract --help
```

This explicit correction step is important: a newer assertion may be corroboration, disagreement,
or replacement, and the storage layer should not guess which one the researcher intended.

## Finished fixture

The full example contains three companies, multiple risks, and public and private evidence:

```bash
uv run examples/cli/investment-opportunities/build.sh /tmp/epiq-investments.sqlite
uv run epiq --db /tmp/epiq-investments.sqlite matrix --kind Company
uv run epiq --db /tmp/epiq-investments.sqlite timeline --kind Company \
  --question amount_raised
```

[schema.json](schema.json) is the repeatable declaration; [writeback.json](writeback.json) is the
atomic agent writeback. They encode the same concepts as the commands above.

<!-- epiq-example -->
```bash
examples/cli/investment-opportunities/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Company \
  --where 'investment_probability >= 0.65'
```
