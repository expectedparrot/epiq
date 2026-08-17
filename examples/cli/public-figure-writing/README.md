# Public figure and writing: model one-to-many data as related rows

This example demonstrates why a person's writings should usually be rows in a separate `Work`
table, not a large JSON array inside one `Person` cell.

The project contains four entity kinds:

```text
Person ──< Work
  │          │
  │          └── Publication
  └── Institution
```

Each work has an `author: Ref[Person]`. Education is a many-valued `Ref[Institution]`. This keeps
every work independently queryable, sourceable, and extensible.

The fixture uses example.test URLs and short synthetic catalog excerpts; it is an executable data
model example, not a researched biographical dataset.

## Build it

```bash
uv run examples/cli/public-figure-writing/build.sh /tmp/epiq-public-writing.sqlite
```

Inspect the person table and writing table separately:

```bash
uv run epiq --db /tmp/epiq-public-writing.sqlite matrix --kind Person
uv run epiq --db /tmp/epiq-public-writing.sqlite matrix --kind Work
uv run epiq --db /tmp/epiq-public-writing.sqlite dossier "Paul Graham"
```

## Query the one-to-many side

Find essays:

```bash
uv run epiq --db /tmp/epiq-public-writing.sqlite query --kind Work \
  --where '{"question":"work_type","op":"eq","value":"essay"}'
```

Find works attributed to Paul Graham. `Ref[Person]` values are stored as stable entity IDs, so first
read the ID from the `Person` matrix and use it in the predicate:

```bash
uv run epiq --db /tmp/epiq-public-writing.sqlite matrix --kind Person
uv run epiq --db /tmp/epiq-public-writing.sqlite query --kind Work \
  --where '{"question":"author","op":"eq","value":"ent_REPLACE_WITH_PERSON_ID"}'
```

View writing chronologically:

```bash
uv run epiq --db /tmp/epiq-public-writing.sqlite timeline \
  --kind Work --question published_date
```

## Extend the model

Because works are entities, later fields can be added without changing the `Person` schema:

```bash
uv run epiq --db /tmp/epiq-public-writing.sqlite question summary \
  --for Work --type String --definition '{"label":"Summary"}'

uv run epiq --db /tmp/epiq-public-writing.sqlite question coauthor \
  --for Work --type 'Ref[Person]' \
  --definition '{"label":"Coauthor","cardinality":"many"}'
```

## What this exercises

- Multiple entity kinds in one project
- Typed cross-table references
- One person related to many independently sourced works
- Many-valued education, occupation, and topic claims
- Structured queries, dossiers, and chronological timelines

