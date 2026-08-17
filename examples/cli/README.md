# Learn Epiq from the command line

These are tutorials, not just sample databases. Each one starts with the question being modeled,
explains how that question becomes entities, fields, evidence, and claims, and then builds the
database one CLI command at a time.

If you are new to Epiq, read them in this order:

1. [Competitor features](competitor-features/README.md) — the basic row, column, evidence, and cell
   model.
2. [Investment opportunities](investment-opportunities/README.md) — numeric types, relationships,
   and beliefs that change.
3. [Hiring committee](hiring-committee/README.md) — private evidence, multiple reviewers, and
   disagreement.
4. [Public figure and writing](public-figure-writing/README.md) — multiple entity kinds and
   one-to-many relationships.

Then use the stress tests to find the edges of the current product:

5. [Forecasting tournament](forecasting-tournament/README.md) — repeated observations and derived scores.
6. [SaaS pricing](saas-pricing/README.md) — multidimensional facts and join entities.
7. [Literature review](literature-review/README.md) — findings, citations, and claims about claims.
8. [Supply-chain risk](supply-chain-risk/README.md) — recursive relationships and risk propagation.
9. [Clinical evidence synthesis](clinical-evidence-synthesis/README.md) — source-linked citations and weighted derived claims.
10. [Procurement normalization](procurement-normalization/README.md) — compound quote identities and landed-cost lineage.
11. [Corporate ownership risk](corporate-ownership-risk/README.md) — multi-row evidence and recursive ownership paths.

Every tutorial has two ways to run it:

- **Learning path:** type the displayed `epiq` commands in order and inspect each result. This is
  deliberately verbose and shows what Epiq records.
- **Fixture path:** run `build.sh` to produce the complete database quickly. The fixture path uses
  `schema.json` and `writeback.json` because those formats are useful for repeatable imports and
  automated tests—not because they are the best introduction to Epiq.

## The four objects to keep in mind

| You want to represent | Epiq object | Spreadsheet analogy |
| --- | --- | --- |
| A company, candidate, person, or work | Entity | Row |
| A typed question about that kind of thing | Question | Column |
| A bounded passage, interview note, or model report | Evidence | Source attached to a cell |
| An answer supported by evidence | Claim | Cell value plus provenance |

The visible matrix is a projection of those objects. Epiq stores the underlying research history,
not merely the latest displayed value.

## Build all finished examples

From the repository root:

```bash
uv run examples/cli/build-all.sh /tmp/epiq-cli-examples
```

The builders are convergent and rerunnable. `apply` skips unchanged entities and questions, while
evidence and claim idempotency prevent duplicates. They never replace an existing project.

The code fences marked `<!-- epiq-example -->` are exercised in CI:

```bash
uv run python scripts/check_markdown_examples.py
```

All companies, candidates, assessments, and investment recommendations in these examples are
synthetic. Public titles in the writing example illustrate relational modeling, not a complete
biographical dataset.
