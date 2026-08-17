# CLI-only Epiq examples

These examples build complete Epiq projects using only the `epiq` CLI. Each directory contains:

- a Markdown tutorial explaining the data model and useful follow-up commands;
- a `build.sh` integration script that creates a new SQLite project; and
- a declarative, idempotent `schema.json` consumed by `epiq apply`; and
- a `writeback.json` atomic evidence-and-claim batch.

Run one example from the repository root:

```bash
uv run examples/cli/competitor-features/build.sh /tmp/epiq-competitors.sqlite
uv run epiq --db /tmp/epiq-competitors.sqlite matrix --kind Product
```

Or build all four into a new directory:

```bash
uv run examples/cli/build-all.sh /tmp/epiq-cli-examples
```

Execute the code fences marked `<!-- epiq-example -->` directly from their Markdown sources:

```bash
uv run python scripts/check_markdown_examples.py
```

The builders are convergent and rerunnable: `apply` skips unchanged schema/entities, while evidence
and claim idempotency prevents duplicate research events. They never delete or replace a project.

| Example | Main concepts exercised |
| --- | --- |
| [Hiring committee](hiring-committee/README.md) | Non-web evidence, multiple reviewers, contested judgments |
| [Investment opportunities](investment-opportunities/README.md) | Quantities, probabilities, entity references, structured queries |
| [Competitor features](competitor-features/README.md) | Comparison matrices, enums, volatility, schema evolution |
| [Public figure and writing](public-figure-writing/README.md) | Multiple tables, one-to-many records, typed relationships, timelines |

All people, committee assessments, and investment recommendations in these fixtures are synthetic.
Public titles used in the writing example are included only to demonstrate relational structure.
