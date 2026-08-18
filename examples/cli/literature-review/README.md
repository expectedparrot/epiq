# Tutorial: model findings—not papers—as the unit of comparison

A paper is a document. A finding is a particular result inside that document. One paper may report
several outcomes and analyses, each with a different effect, sample, confidence interval, and page
or table locator.

If every paper were one row, fields such as `effect_size` would become ambiguous. If every result
were stuffed into one JSON cell, it would be difficult to filter, compare, cite, or challenge.
Epiq therefore models this review as two related tables:

```text
Paper  <── paper ──  Finding
                       ├── outcome
                       ├── analysis
                       ├── effect_size
                       ├── sample_size
                       └── confidence interval
```

The example is synthetic and teaches data mechanics rather than making claims about remote work.

## 1. Create the project and its rows

```bash
epiq use /tmp/epiq-literature.sqlite
epiq init --name "Remote work literature review"

epiq entity Paper "Remote Work Study A"
epiq entity Paper "Remote Work Study B"

epiq entity Finding "Study A productivity primary" --role observation \
  --identity '{"paper":"Remote Work Study A","outcome":"productivity","analysis":"primary"}'
epiq entity Finding "Study A retention primary" --role observation \
  --identity '{"paper":"Remote Work Study A","outcome":"retention","analysis":"primary"}'
epiq entity Finding "Study A manager subgroup" --role observation \
  --identity '{"paper":"Remote Work Study A","outcome":"productivity","analysis":"manager_subgroup"}'
epiq entity Finding "Study B productivity primary" --role observation \
  --identity '{"paper":"Remote Work Study B","outcome":"productivity","analysis":"primary"}'
```

`Paper` and `Finding` are separate tables. The four `Finding` entities are rows, and `--role
observation` says each row represents a reported result rather than a durable real-world object.

## 2. Define the finding columns

```bash
epiq question paper --for Finding --type 'Ref[Paper]' \
  --definition '{"label":"Paper"}'
epiq question outcome --for Finding --type 'Enum[productivity,retention]' \
  --definition '{"label":"Outcome"}'
epiq question analysis --for Finding --type 'Enum[primary,manager_subgroup]' \
  --definition '{"label":"Analysis"}'
epiq question effect_size --for Finding --type Float \
  --definition '{"label":"Standardized effect"}'
epiq question sample_size --for Finding --type Int \
  --definition '{"label":"Sample size"}'
epiq question ci_lower --for Finding --type Float \
  --definition '{"label":"95% CI lower"}'
epiq question ci_upper --for Finding --type Float \
  --definition '{"label":"95% CI upper"}'
```

At this point `epiq matrix --kind Finding` shows four rows whose cells are all `Unasked`. The schema
describes what can be learned; it does not pretend the research has already happened.

## 3. Record each result with its exact location

One bounded passage supports all the cells for the first finding:

```bash
epiq --actor agent:review record \
  --subject "Study A productivity primary" \
  --source-type web --url "https://example.test/papers/remote-work-a" \
  --source-title "Remote Work Study A" --retrieved-at 2026-08-17 \
  --source-entity "Remote Work Study A" \
  --locator '{"page":12,"table":"2","outcome":"productivity"}' \
  --excerpt 'The primary productivity analysis reports an effect of 0.18 (95% CI 0.08 to 0.28; N=420).' \
  --valid-from 2025-01-01 \
  --answer paper "Remote Work Study A" \
  --answer outcome productivity --answer analysis primary \
  --answer effect_size 0.18 --answer sample_size 420 \
  --answer ci_lower 0.08 --answer ci_upper 0.28
```

Record the other three findings the same way, changing the locator and values for each passage:

```bash
epiq --actor agent:review record \
  --subject "Study A retention primary" \
  --source-type web --url "https://example.test/papers/remote-work-a" \
  --source-title "Remote Work Study A" --retrieved-at 2026-08-17 \
  --source-entity "Remote Work Study A" \
  --locator '{"page":16,"table":"4","outcome":"retention"}' \
  --excerpt 'The primary retention analysis reports an effect of 0.07 (95% CI -0.02 to 0.16; N=420).' \
  --valid-from 2025-01-01 \
  --answer paper "Remote Work Study A" \
  --answer outcome retention --answer analysis primary \
  --answer effect_size 0.07 --answer sample_size 420 \
  --answer ci_lower -0.02 --answer ci_upper 0.16

epiq --actor agent:review record \
  --subject "Study A manager subgroup" \
  --source-type web --url "https://example.test/papers/remote-work-a" \
  --source-title "Remote Work Study A" --retrieved-at 2026-08-17 \
  --source-entity "Remote Work Study A" \
  --locator '{"page":14,"table":"3","outcome":"productivity","subgroup":"managers"}' \
  --excerpt 'For managers, the productivity effect is -0.03 (95% CI -0.18 to 0.12; N=90).' \
  --valid-from 2025-01-01 \
  --answer paper "Remote Work Study A" \
  --answer outcome productivity --answer analysis manager_subgroup \
  --answer effect_size -0.03 --answer sample_size 90 \
  --answer ci_lower -0.18 --answer ci_upper 0.12

epiq --actor agent:review record \
  --subject "Study B productivity primary" \
  --source-type web --url "https://example.test/papers/remote-work-b" \
  --source-title "Remote Work Study B" --retrieved-at 2026-08-17 \
  --source-entity "Remote Work Study B" \
  --locator '{"page":9,"table":"2","outcome":"productivity"}' \
  --excerpt 'The primary productivity analysis reports an effect of -0.05 (95% CI -0.12 to 0.02; N=610).' \
  --valid-from 2025-06-01 \
  --answer paper "Remote Work Study B" \
  --answer outcome productivity --answer analysis primary \
  --answer effect_size -0.05 --answer sample_size 610 \
  --answer ci_lower -0.12 --answer ci_upper 0.02
```

Each `record` call writes one evidence passage and seven typed claims atomically. A malformed value
rolls back the whole record instead of leaving a partially populated finding.

Now inspect the table you built:

```bash
epiq --format table matrix --kind Finding
```

| Finding | Paper | Outcome | Analysis | Effect | N | 95% CI |
| --- | --- | --- | --- | ---: | ---: | --- |
| Study A productivity primary | Remote Work Study A | productivity | primary | 0.18 | 420 | 0.08, 0.28 |
| Study A retention primary | Remote Work Study A | retention | primary | 0.07 | 420 | -0.02, 0.16 |
| Study A manager subgroup | Remote Work Study A | productivity | manager subgroup | -0.03 | 90 | -0.18, 0.12 |
| Study B productivity primary | Remote Work Study B | productivity | primary | -0.05 | 610 | -0.12, 0.02 |

The first three rows come from one paper. They show why “What did Study A find?” does not have one
scalar answer: the answer depends on which outcome, analysis, and population you mean.

## 4. Understand the compound identity

Each `Finding` is an `observation` whose identity is `(paper, outcome, analysis)`:

```bash
epiq entity Finding "Study A productivity primary" --role observation \
  --identity '{"paper":"Remote Work Study A","outcome":"productivity","analysis":"primary"}'
```

The row already exists, so repeating the command resolves to its existing ID. Re-running an agent
import cannot create a duplicate merely because it phrases the display name differently.

The identity also prevents two distinct Study A results from being conflated:

- primary productivity;
- primary retention; and
- productivity for the manager subgroup.

## 5. Traverse from a paper to its independently sourced findings

```bash
epiq --format table related "Remote Work Study A" \
  --via paper --direction incoming
```

The result has three incoming edges—one from each Study A finding. The paper remains a useful row
for authorship and bibliographic metadata, while its findings remain separately inspectable.

```bash
epiq dossier "Study A manager subgroup"
```

The dossier shows the exact supporting passage and structured locator:

```json
{
  "page": 14,
  "table": "3",
  "outcome": "productivity",
  "subgroup": "managers"
}
```

That locator belongs to this finding's evidence—not vaguely to the paper as a whole.

## 6. Select comparable findings

To compare the primary productivity results across papers, filter on both the outcome and analysis:

```bash
epiq query --kind Finding \
  --where 'outcome=productivity' --where 'analysis=primary'
```

Output: `matched: 2`, returning effects `0.18` and `-0.05`. Filtering only on productivity would
also return the manager subgroup and mix estimands that may not be comparable:

```bash
epiq query --kind Finding --where 'outcome=productivity'
```

That broader query reports `matched: 3`.

## 7. Aggregate carefully

Suppose we group effect sizes by analysis type:

```bash
epiq --format table aggregate --kind Finding \
  --question effect_size --op avg --group-by analysis
```

| analysis | mean effect | count |
| --- | ---: | ---: |
| primary | 0.0667 | 3 |
| manager_subgroup | -0.03 | 1 |

This groups every outcome, so even the primary row mixes productivity and retention. The command is
mathematically valid but not automatically a valid research synthesis. Epiq currently cannot feed
a filtered query directly into `aggregate`, and an unweighted mean ignores sampling uncertainty.

The [clinical evidence synthesis tutorial](../clinical-evidence-synthesis/README.md) shows a
persisted weighted derivation with claim lineage. A serious meta-analysis would additionally need
standard errors, an explicit model, compatibility rules, and heterogeneity diagnostics.

## What this example demonstrates

- Papers and findings are different entity kinds with a one-to-many relationship.
- A result's compound identity prevents duplicate ingestion.
- Evidence can link to its Paper entity while retaining a finding-specific page/table locator.
- Queries can select findings by outcome and analysis rather than treating a whole paper as one
  answer.
- The schema preserves enough structure to reveal when an aggregation is scientifically
  inappropriate instead of making the aggregation look authoritative.

## Product gaps surfaced

- `aggregate` cannot consume a saved or filtered query as its input population.
- Confidence intervals are separate fields rather than a reusable estimate-with-uncertainty type.
- There is no first-class meta-analysis model, replication relationship, or claim-about-claim.
- Evidence-quality rubrics remain project-defined fields or assessments rather than reusable
  schemas.
