# Tutorial: represent Paul Graham and his writing as related tables

Suppose you want biographical details about Paul Graham and a catalog of his writing. Putting all
works in one `Person.writings` cell would make each work difficult to cite, query, date, or extend.
Instead, model works as rows in their own table and connect them to the person.

```text
Person  <── author ──  Work  ── published_in ──>  Publication
  └──────────── educated_at ───────────────────>  Institution
```

This tutorial uses public pages on `paulgraham.com`. Epiq does not fetch them: the commands show
how a human or research agent records the passages it inspected and the claims those passages
support.

## 1. Create the project and its rows

```bash
epiq use /tmp/paul-graham-writing.sqlite
epiq init --name "Paul Graham and his writing"

epiq entity Person "Paul Graham"
epiq entity Institution "Cornell University"
epiq entity Institution "Harvard University"
epiq entity Publication "paulgraham.com"
epiq entity Work "Hackers & Painters"
epiq entity Work "How to Start a Startup"
epiq entity Work "Maker's Schedule, Manager's Schedule"
```

`epiq use` stores the active database selection for this workspace. `epiq db` shows what is
selected, and an explicit `--db` can still override it for one command.

An entity kind is analogous to a table. The entities are its rows. A book and two essays therefore
become three independently researchable `Work` rows rather than an array hidden in the person row.

## 2. Define biographical and bibliographical fields

```bash
epiq question educated_at --for Person --type 'Ref[Institution]' \
  --definition '{"label":"Education","cardinality":"many"}'

epiq question occupation --for Person --type String \
  --definition '{"label":"Occupation","cardinality":"many"}'

epiq question author --for Work --type 'Ref[Person]' \
  --definition '{"label":"Author","cardinality":"many"}'

epiq question published_in --for Work --type 'Ref[Publication]' \
  --definition '{"label":"Publication","cardinality":"one"}'

epiq question published_year --for Work --type Year \
  --definition '{"label":"Publication year","cardinality":"one"}'

epiq question work_type --for Work --type 'Enum[essay,book]' \
  --definition '{"label":"Work type","cardinality":"one"}'

epiq question canonical_url --for Work --type URL \
  --definition '{"label":"Canonical URL","cardinality":"one"}'

epiq question topic --for Work --type String \
  --definition '{"label":"Topic","cardinality":"many"}'
```

`Ref[Person]` is not arbitrary text. Epiq validates that the target exists and is a `Person`, then
stores its durable entity ID. `cardinality: many` allows coauthors, several institutions, or
multiple topics without overwriting an earlier value.

At this point the tables contain rows and typed columns, but their cells remain `Unasked`:

```bash
epiq matrix --kind Person
epiq matrix --kind Work
```

| Person | Education | Occupation |
| --- | --- | --- |
| Paul Graham | Unasked | Unasked |

| Work | Author | Publication | Publication year | Work type | Canonical URL | Topic |
| --- | --- | --- | ---: | --- | --- | --- |
| Hackers & Painters | Unasked | Unasked | Unasked | Unasked | Unasked | Unasked |
| How to Start a Startup | Unasked | Unasked | Unasked | Unasked | Unasked | Unasked |
| Maker's Schedule, Manager's Schedule | Unasked | Unasked | Unasked | Unasked | Unasked | Unasked |

## 3. Record biography evidence once and use it for several claims

Paul Graham's official biography describes him as a programmer, writer, and investor and lists an
AB from Cornell and a PhD in Computer Science from Harvard. One bounded passage can support all
five claims atomically:

```bash
epiq --actor agent:catalog record \
  --subject "Paul Graham" \
  --source-type web \
  --url "https://www.paulgraham.com/bio.html" \
  --source-title "Paul Graham: Bio" \
  --retrieved-at 2026-08-18 \
  --excerpt "Paul Graham is a programmer, writer, and investor. He has an AB from Cornell and a PhD in Computer Science from Harvard." \
  --valid-from 2026-08-18 \
  --answer educated_at "Cornell University" \
  --answer educated_at "Harvard University" \
  --answer occupation "Programmer" \
  --answer occupation "Writer" \
  --answer occupation "Investor"
```

The evidence is stored once. Each answer is a separate typed claim, so one occupation or education
claim can later be challenged without retracting the others.

## 4. Source and connect the works

The official book page identifies *Hackers & Painters* as a 2004 O'Reilly book. Record the source
and every answer it supports in one transaction:

```bash
epiq --actor agent:catalog record \
  --subject "Hackers & Painters" \
  --source-type web \
  --url "https://www.paulgraham.com/hackpaint.html" \
  --source-title "Hackers & Painters" \
  --retrieved-at 2026-08-18 \
  --excerpt "Hackers & Painters, by Paul Graham. O'Reilly, 2004." \
  --valid-from 2004-01-01 \
  --answer author "Paul Graham" \
  --answer work_type book \
  --answer published_year 2004 \
  --answer topic "Programming" \
  --answer topic "Startups" \
  --answer canonical_url "https://www.paulgraham.com/hackpaint.html"
```

The essay pages supply their own month and year:

```bash
epiq --actor agent:catalog record \
  --subject "How to Start a Startup" \
  --source-type web \
  --url "https://www.paulgraham.com/start.html" \
  --source-title "How to Start a Startup" \
  --retrieved-at 2026-08-18 \
  --excerpt "How to Start a Startup, by Paul Graham. March 2005." \
  --valid-from 2005-03-01 \
  --answer author "Paul Graham" \
  --answer published_in "paulgraham.com" \
  --answer published_year 2005 \
  --answer work_type essay \
  --answer topic "Startup formation" \
  --answer canonical_url "https://www.paulgraham.com/start.html"

epiq --actor agent:catalog record \
  --subject "Maker's Schedule, Manager's Schedule" \
  --source-type web \
  --url "https://www.paulgraham.com/makersschedule.html" \
  --source-title "Maker's Schedule, Manager's Schedule" \
  --retrieved-at 2026-08-18 \
  --excerpt "Maker's Schedule, Manager's Schedule, by Paul Graham. July 2009." \
  --valid-from 2009-07-01 \
  --answer author "Paul Graham" \
  --answer published_in "paulgraham.com" \
  --answer published_year 2009 \
  --answer work_type essay \
  --answer topic "Work and management" \
  --answer canonical_url "https://www.paulgraham.com/makersschedule.html"
```

Now the work table has independently sourced rows:

```bash
epiq --format table matrix --kind Work
```

| Work | Author | Publication | Year | Type | Topic |
| --- | --- | --- | ---: | --- | --- |
| Hackers & Painters | Paul Graham | Unasked | 2004 | book | Programming; Startups |
| How to Start a Startup | Paul Graham | paulgraham.com | 2005 | essay | Startup formation |
| Maker's Schedule, Manager's Schedule | Paul Graham | paulgraham.com | 2009 | essay | Work and management |

`Unasked` is meaningful: the book has a publisher, but this schema's `published_in` relationship
points to a `Publication` row and no O'Reilly entity or relationship claim has been added yet. Epiq
does not silently convert missing modeling work into a guessed value.

## 5. Traverse the one-to-many relationship

```bash
epiq dossier "How to Start a Startup"
epiq related "Paul Graham" --via author --direction incoming
epiq query --kind Work --where 'author=Paul Graham'
epiq timeline --kind Work --question published_year
```

`author` lives on each `Work`, so works are incoming relationships from Paul Graham's perspective.
Both `related` and `query` return three works; `query` also projects their remaining fields. The
timeline orders the three publication claims as 2004, 2005, and 2009.

This is why works deserve rows: each can gain its own topics, citations, summary, URL, evidence,
and challenge history without changing or expanding the `Person` record.

## Build the finished fixture

The packaged fixture performs the same writes in a reproducible form:

```bash
uv run examples/cli/public-figure-writing/build.sh /tmp/epiq-public-writing.sqlite
uv run epiq --db /tmp/epiq-public-writing.sqlite --format table matrix --kind Person
uv run epiq --db /tmp/epiq-public-writing.sqlite --format table matrix --kind Work
uv run epiq --db /tmp/epiq-public-writing.sqlite related "Paul Graham" \
  --via author --direction incoming
```

After learning the commands, compare them with [schema.json](schema.json) and
[writeback.json](writeback.json). Those files express the same model for repeatable imports and
agent write-back; they are not a different database abstraction.

<!-- epiq-example -->
```bash
examples/cli/public-figure-writing/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select count related "Paul Graham" \
  --via author --direction incoming
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Work \
  --where 'author=Paul Graham'
```
