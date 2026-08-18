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

## 1. Build the review and inspect its findings

```bash
uv run examples/cli/literature-review/build.sh /tmp/epiq-literature.sqlite
uv run epiq use /tmp/epiq-literature.sqlite
uv run epiq --format table matrix --kind Finding
```

| Finding | Paper | Outcome | Analysis | Effect | N | 95% CI |
| --- | --- | --- | --- | ---: | ---: | --- |
| Study A productivity primary | Remote Work Study A | productivity | primary | 0.18 | 420 | 0.08, 0.28 |
| Study A retention primary | Remote Work Study A | retention | primary | 0.07 | 420 | -0.02, 0.16 |
| Study A manager subgroup | Remote Work Study A | productivity | manager subgroup | -0.03 | 90 | -0.18, 0.12 |
| Study B productivity primary | Remote Work Study B | productivity | primary | -0.05 | 610 | -0.12, 0.02 |

The first three rows come from one paper. This is the distinction the original version failed to
show: “What did Study A find?” does not have one scalar answer.

## 2. Give every result a stable compound identity

Each `Finding` is an `observation` whose identity is `(paper, outcome, analysis)`:

```bash
uv run epiq entity Finding "Study A productivity primary" --role observation \
  --identity '{"paper":"Remote Work Study A","outcome":"productivity","analysis":"primary"}'
```

The fixture already contains this row, so the command resolves to its existing ID. Re-running an
agent import cannot create a duplicate merely because it phrases the display name differently.

The identity also prevents two distinct Study A results from being conflated:

- primary productivity;
- primary retention; and
- productivity for the manager subgroup.

## 3. Traverse from a paper to its independently sourced findings

```bash
uv run epiq --format table related "Remote Work Study A" \
  --via paper --direction incoming
```

The result has three incoming edges—one from each Study A finding. The paper remains a useful row
for authorship and bibliographic metadata, while its findings remain separately inspectable.

```bash
uv run epiq dossier "Study A manager subgroup"
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

## 4. Select comparable findings

To compare the primary productivity results across papers, filter on both the outcome and analysis:

```bash
uv run epiq query --kind Finding \
  --where 'outcome=productivity' --where 'analysis=primary'
```

Output: `matched: 2`, returning effects `0.18` and `-0.05`. Filtering only on productivity would
also return the manager subgroup and mix estimands that may not be comparable:

```bash
uv run epiq query --kind Finding --where 'outcome=productivity'
```

That broader query reports `matched: 3`.

## 5. Aggregate carefully

Epiq can calculate a descriptive mean over the two primary productivity rows:

```bash
uv run epiq --format table aggregate --kind Finding \
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

<!-- epiq-example -->
```bash
examples/cli/literature-review/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq --select count related "Remote Work Study A" --via paper --direction incoming
epiq --select query.matched query --kind Finding \
  --where 'outcome=productivity' --where 'analysis=primary'
epiq --select query.matched query --kind Finding --where 'outcome=productivity'
epiq --select groups aggregate --kind Finding --question effect_size --op avg \
  --group-by analysis
```
