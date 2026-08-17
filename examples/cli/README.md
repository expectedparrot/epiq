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

Then use the advanced tutorials to exercise combinations of those primitives:

5. [Forecasting tournament](forecasting-tournament/README.md) — repeated observations, aggregation,
   derived ensembles, and dynamic-cohort limits.
6. [SaaS pricing](saas-pricing/README.md) — multidimensional facts and join entities.
7. [Literature review](literature-review/README.md) — findings, citations, and claims about claims.
8. [Supply-chain risk](supply-chain-risk/README.md) — recursive relationships and risk propagation.
9. [Clinical evidence synthesis](clinical-evidence-synthesis/README.md) — source-linked citations,
   claim-backed weights, and stale derivations.
10. [Procurement normalization](procurement-normalization/README.md) — compound quote identities,
    declarative table formulas, and landed-cost lineage.
11. [Corporate ownership risk](corporate-ownership-risk/README.md) — multi-row evidence, recursive
    ownership paths, propagation, and path staleness.

If you are looking for one capability rather than a domain, use this map:

| Capability | Best tutorial |
| --- | --- |
| First entities, fields, evidence, and claims | Competitor features |
| Multiple reviewers and contested cells | Hiring committee |
| Observation and relation rows with compound identity | Forecasting or SaaS pricing |
| One source supporting cells on multiple rows | SaaS pricing or corporate ownership |
| Structured source locators and source entities | Literature or clinical evidence |
| Declarative per-row formulas | Procurement normalization |
| Claim-backed calculation parameters | Clinical evidence synthesis |
| Recursive mixed-edge traversal and propagation | Supply-chain risk |
| Dependency roles and stale derivations | Clinical, procurement, or ownership risk |

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

## Let an agent discover the tool

An agent does not need to scrape this documentation. These commands expose the versioned CLI
protocol and then the selected project's schema and beliefs:

```bash
epiq capabilities --command record
epiq capabilities --include-schema
epiq context --budget 4000
```

The first command works before a project exists. It describes `record` arguments, constraints,
transaction behavior, return fields, and a complete example. The latter commands add the live row
types, questions, values, confidence, and provenance needed for a research session.

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
