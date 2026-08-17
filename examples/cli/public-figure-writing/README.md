# Tutorial: represent a person and their writing without a giant JSON cell

Suppose you want biographical details about a public figure and a list of their writings. Putting
all writings in one `Person.writings` cell makes each work difficult to cite, query, date, or extend.
Instead, model works as rows in their own table and connect them to the person.

```text
Person  <── author ──  Work  ── published_in ──>  Publication
  └──────────── educated_at ───────────────────>  Institution
```

The source excerpts below are synthetic and use `example.test`; this teaches the structure rather
than claiming to be a researched biography.

## 1. Create several kinds of row

```bash
epiq use /tmp/writing.sqlite
epiq init --name "Public writing tutorial"
epiq entity Person "Ada Example"
epiq entity Work "Notes on Small Systems"
epiq entity Work "Scaling Tiny Teams"
epiq entity Publication "Example Review"
epiq entity Institution "Example University"
```

`epiq use` stores the active database selection for this workspace. `epiq db` shows what is
selected, and an explicit `--db` can still override it for a single command.

An entity kind is analogous to a table. The entities are its rows.

## 2. Define relationship columns

```bash
epiq question author --for Work --type 'Ref[Person]' \
  --definition '{"label":"Author","cardinality":"many"}'

epiq question published_in --for Work \
  --type 'Ref[Publication]' \
  --definition '{"label":"Publication","cardinality":"one"}'

epiq question published_date --for Work --type Date \
  --definition '{"label":"Publication date","cardinality":"one"}'

epiq question educated_at --for Person \
  --type 'Ref[Institution]' \
  --definition '{"label":"Education","cardinality":"many"}'
```

`Ref[Person]` is not arbitrary text. Epiq validates that the target exists and is a `Person`, then
stores its durable ID. `cardinality: many` allows a coauthored work or several institutions.

The two entity kinds project independently:

```bash
epiq matrix --kind Person
epiq matrix --kind Work
```

| Person | Education |
| --- | --- |
| Ada Example | Unasked |

| Work | Author | Publication | Publication date |
| --- | --- | --- | --- |
| Notes on Small Systems | Unasked | Unasked | Unasked |
| Scaling Tiny Teams | Unasked | Unasked | Unasked |

## 3. Source and connect one work

```bash
epiq --actor agent:catalog record \
  --subject "Notes on Small Systems" \
  --source-type web \
  --url "https://example.test/catalog/notes-small-systems" \
  --source-title "Example Review catalog record" \
  --retrieved-at 2026-08-17 \
  --excerpt "Notes on Small Systems, by Ada Example, appeared in Example Review on 2024-05-03." \
  --valid-from 2024-05-03 \
  --answer author "Ada Example" \
  --answer published_in "Example Review" \
  --answer published_date 2024-05-03

epiq --actor agent:catalog record \
  --subject "Scaling Tiny Teams" \
  --source-type web \
  --url "https://example.test/catalog/scaling-tiny-teams" \
  --source-title "Example Review catalog record" \
  --retrieved-at 2026-08-17 \
  --excerpt "Scaling Tiny Teams, by Ada Example, appeared in Example Review on 2025-02-12." \
  --valid-from 2025-02-12 \
  --answer author "Ada Example" \
  --answer published_in "Example Review" \
  --answer published_date 2025-02-12
```

Each command returns `answer_count: 3`, one evidence ID, and three claim IDs. Each catalog passage
supports three distinct claims that can later be corroborated, challenged, or superseded
independently.

```bash
epiq matrix --kind Work
```

| Work | Author | Publication | Publication date |
| --- | --- | --- | --- |
| Notes on Small Systems | Ada Example | Example Review | 2024-05-03 |
| Scaling Tiny Teams | Ada Example | Example Review | 2025-02-12 |

## 4. Traverse the relationship in both directions

```bash
epiq dossier "Notes on Small Systems"
epiq related "Ada Example" --via author --direction incoming
epiq query --kind Work --where 'author=Ada Example'
```

`author` lives on each `Work`, so works are incoming relationships from the person's perspective.
The query accepts a name; Epiq resolves it to the stable entity ID.

The dossier lineage for the first work includes:

| Field | Value | Actor | Evidence |
| --- | --- | --- | --- |
| Author | Ada Example | `agent:catalog` | Example Review catalog record |
| Publication | Example Review | `agent:catalog` | Example Review catalog record |
| Publication date | 2024-05-03 | `agent:catalog` | Example Review catalog record |

`related` shows the inverse traversal (IDs abbreviated):

```json
{
  "count": 2,
  "direction": "incoming",
  "entity": {"kind": "Person", "name": "Ada Example"},
  "via": "author",
  "edges": [
    {"direction": "incoming", "question": "author", "from": {"kind": "Work", "name": "Notes on Small Systems"}},
    {"direction": "incoming", "question": "author", "from": {"kind": "Work", "name": "Scaling Tiny Teams"}}
  ]
}
```

The query returns the same two Work rows and reports `"matched": 2`; unlike `related`, it also
projects their other fields.

## 5. See why rows are more extensible than an array cell

You can now add fields that apply independently to every work:

```bash
epiq question work_type --for Work \
  --type 'Enum[essay,book,talk,paper,other]' \
  --definition '{"label":"Work type","cardinality":"one"}'

epiq question topic --for Work --type String \
  --definition '{"label":"Topic","cardinality":"many"}'

epiq --actor agent:catalog record \
  --subject "Notes on Small Systems" \
  --source-type web \
  --url "https://example.test/catalog/notes-small-systems" \
  --source-title "Example Review catalog record" \
  --retrieved-at 2026-08-17 \
  --excerpt "Notes on Small Systems is an essay about software architecture and small teams." \
  --valid-from 2024-05-03 \
  --answer work_type essay \
  --answer topic "Software architecture" \
  --answer topic "Small teams"

epiq timeline --kind Work --question published_date
```

The timeline output orders the two dated claims:

```json
{
  "entity_kind": "Work",
  "observations": [
    {"entity_name": "Notes on Small Systems", "value": "2024-05-03", "as_of": "2024-05-03"},
    {"entity_name": "Scaling Tiny Teams", "value": "2025-02-12", "as_of": "2025-02-12"}
  ],
  "question": "published_date"
}
```

Each work can acquire its own topics, summary, citations, date, and challenge history without
changing the `Person` record.

## Finished fixture

The packaged version uses Paul Graham as a familiar structural example and contains several works:

```bash
uv run examples/cli/public-figure-writing/build.sh /tmp/epiq-public-writing.sqlite
uv run epiq --db /tmp/epiq-public-writing.sqlite matrix --kind Person
uv run epiq --db /tmp/epiq-public-writing.sqlite matrix --kind Work
uv run epiq --db /tmp/epiq-public-writing.sqlite related "Paul Graham" \
  --via author --direction incoming
```

After learning the commands, compare them with [schema.json](schema.json) and
[writeback.json](writeback.json). Those files make the same model convenient to reproduce or feed
from an agent; they are not a different database abstraction.

<!-- epiq-example -->
```bash
examples/cli/public-figure-writing/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select count related "Paul Graham" \
  --via author --direction incoming
```
