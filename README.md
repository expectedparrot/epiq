# Epiq

![Epiq — an agentic epistemic database](assets/epiq-hero.png)

Epiq is a local-first epistemic database for agent-driven research. It stores entities, typed
questions, source excerpts, and evidence-backed claims in SQLite. It can then project that history
into ordinary tables, interactive HTML, and Excel without throwing away where each cell came from.

Epiq does not search the web or call a language model. A person, script, or research agent finds
information and submits it through the same deterministic interface. This keeps research
orchestration replaceable and makes storage behavior testable.

This README builds a database from scratch before introducing the packaged examples.

## The model in one minute

Suppose you want a table like this:

| Town | Population | Median home value |
| --- | ---: | ---: |
| Barnstable | 49,568 | $602,500 |
| Truro | 1,708 | $888,200 |

An ordinary spreadsheet stores the displayed values. Epiq stores the pieces that justify them:

| Epiq object | Example | Rough spreadsheet analogy |
| --- | --- | --- |
| Entity | `Barnstable`, of kind `Town` | Row |
| Question | `population : Int for Town` | Typed column |
| Source | Census API URL and retrieval date | Citation |
| Evidence | A bounded excerpt from that source | Supporting passage |
| Claim | Barnstable's population was 49,568 as of 2024-12-31 | Cell assertion |
| Event | `claim.assert` by `agent:census` | Audit-log entry |
| Projection | Current Town-by-question matrix | View or report |

The table is derived. Claims and evidence are the durable record.

Four cell states remain distinct:

- `Answered`: one supported current answer.
- `Contested`: multiple incompatible current answers.
- `NotFound`: a bounded search was completed without sufficient evidence.
- `Unasked`: no claim or completed search has been recorded.

`NotFound` is deliberately not a negative answer. “I searched and could not establish whether the
product supports SSO” does not mean “the product does not support SSO.”

## Install

Epiq requires Python 3.11 or later and has no runtime dependencies.

From a checkout:

```bash
uv sync --extra test
uv run epiq --help
```

Install the command in an isolated environment:

```bash
uv tool install .
epiq --help
```

You can also replace `epiq` with `python -m epiq` in every example below.

## Tutorial: build a town database

### 1. Select a database

Choose the SQLite file once for the current workspace:

```bash
epiq use examples/tutorial-towns.sqlite
```

Epiq writes the absolute path to `.epiq/config.json`. The file is workspace configuration, is
ignored by Git, and may point to a database that does not exist yet.

Inspect the selection:

```bash
epiq db
```

```json
{
  "database": "/path/to/epiq/examples/tutorial-towns.sqlite",
  "exists": false,
  "ok": true,
  "source": "workspace"
}
```

Database resolution has an explicit precedence order:

1. `epiq --db path/to/file.sqlite ...`
2. The `EPIQ_DB` environment variable
3. `.epiq/config.json`, written by `epiq use`
4. `.epiq/epiq.sqlite`

Therefore CI can use `EPIQ_DB`, a developer can use `epiq use`, and an individual command can
still override both.

### 2. Initialize it

```bash
epiq init --name "Cape Cod Town Tutorial"
```

```json
{
  "database": "/path/to/epiq/examples/tutorial-towns.sqlite",
  "name": "Cape Cod Town Tutorial",
  "ok": true
}
```

Initialization creates the SQLite schema and immutable project identity. Running `init` again on
the same path fails rather than replacing the database.

### 3. Add entities—the future rows

```bash
epiq entity Town "Barnstable" \
  --attributes '{"county":"Barnstable County","state":"Massachusetts","geoid":"06000US2500103690"}'

epiq entity Town "Truro" \
  --attributes '{"county":"Barnstable County","state":"Massachusetts","geoid":"06000US2500170605"}'
```

Each command returns a stable ID:

```json
{
  "entity_id": "ent_...",
  "ok": true
}
```

Commands accept either that ID or the exact entity name when referring to the entity later.
Attributes are descriptive metadata; researched values belong in claims, not attributes.

Entity identity can evolve without erasing or rewriting research:

```bash
# Let later commands and agents resolve an alternate name.
epiq entity-alias "Barnstable" "Town of Barnstable"

# Unify a duplicate row into the surviving row; historical claim subject IDs remain intact.
epiq merge-entities "Barnstable, MA" "Barnstable" --reason "Duplicate place identity"

# Remove a row from current projections, then restore it if the scope changes.
epiq retire-entity "Truro" --reason "Outside current comparison scope"
epiq restore-entity "Truro" --reason "Restored to comparison scope"
```

### 4. Define typed questions—the future columns

Questions apply to an entity kind. Adding a question is how the schema grows.

```bash
epiq question population \
  --for Town \
  --type Int \
  --definition '{"label":"Population estimate","unit":"people","cardinality":"one"}'

epiq question median_home_value \
  --for Town \
  --type Int \
  --definition '{"label":"Median owner-occupied home value","unit":"USD","cardinality":"one"}'
```

The question name is the stable machine-facing field name. `definition` holds presentation and
policy metadata. The current implementation recognizes:

- `Int`: validated as a JSON integer.
- `Float`: validated as a finite JSON number; integers are accepted because they are valid real
  values (for example, probability endpoints `0` and `1`).
- `Probability`: a finite number constrained to the closed interval `[0,1]`.
- `Bool`: validated as JSON `true` or `false`.
- `String`: validated as plain text.
- `Enum[a,b,c]`: validated against the listed strings.
- `Distribution[Float]`: an empirical or weighted empirical numeric distribution.
- `Distribution[Enum[a,b,c]]`: a categorical probability distribution over exactly those outcomes.
- `Ref[EntityKind]`: a validated relationship to another entity; names and aliases resolve to a
  stable entity ID when the claim is recorded.
- `Quantity[unit]`: a finite numeric measurement whose unit is part of the field's declared type,
  such as `Quantity[USD]`, `Quantity[people]`, or `Quantity[km^2]`.
- `Json`: accepts structured JSON for richer answers.

For example, probability and free-text fields can be declared without wrapping either in a JSON
object:

```bash
epiq question probability_of_launch \
  --for Company \
  --type Probability \
  --definition '{"label":"Probability of launch","cardinality":"one"}'

epiq question positioning_summary \
  --for Company \
  --type String \
  --definition '{"label":"Positioning summary","cardinality":"one"}'
```

`cardinality` defaults to `one`. A question with `"cardinality":"many"` projects all supported
values instead of treating multiple values as a contradiction.

Questions are immutable and versioned. Defining the same question name again creates a new version;
current projections use the latest version.

### 5. Add evidence

Evidence is stored before a claim can cite it:

```bash
epiq --actor agent:census evidence \
  --url "https://api.example.gov/towns/barnstable" \
  --title "2024 town estimates" \
  --retrieved-at 2026-08-15 \
  --excerpt "Barnstable population: 49,568; median owner-occupied home value: $602,500."
```

```json
{
  "evidence_id": "evd_...",
  "ok": true,
  "source_id": "src_..."
}
```

A source records the URL, title, retrieval date, and content hash. Evidence is the bounded excerpt
used to support a claim. Repeating the same URL and excerpt returns the existing IDs, which makes
agent retries safe.

A claim may cite more than one fragment by repeating `--evidence`:

```bash
epiq assert --subject Barnstable --question population --value 49568 \
  --valid-from 2024-12-31 \
  --evidence evd_census_table \
  --evidence evd_town_profile
```

Every evidence link remains visible in JSON, HTML, and Excel lineage.

Capture the evidence ID for shell chaining with `jq`:

```bash
BARNSTABLE_EVIDENCE=$(epiq --actor agent:census evidence \
  --url "https://api.example.gov/towns/barnstable" \
  --title "2024 town estimates" \
  --retrieved-at 2026-08-15 \
  --excerpt "Barnstable population: 49,568; median owner-occupied home value: $602,500." \
  | jq -r .evidence_id)
```

The CLI does not download or summarize the URL. The caller is responsible for retrieval; Epiq
stores the submitted source metadata and excerpt.

### 6. Assert evidence-backed claims

Use the same evidence fragment for both facts it supports:

```bash
epiq --actor agent:census assert \
  --subject "Barnstable" \
  --question population \
  --value 49568 \
  --valid-from 2024-12-31 \
  --evidence "$BARNSTABLE_EVIDENCE" \
  --confidence high

epiq --actor agent:census assert \
  --subject "Barnstable" \
  --question median_home_value \
  --value 602500 \
  --valid-from 2024-12-31 \
  --evidence "$BARNSTABLE_EVIDENCE" \
  --confidence high
```

An assertion is rejected if the entity, question, or evidence does not exist; the question applies
to another entity kind; or the value fails type validation. Confidence is `low`, `medium`, or
`high`. Epiq records confidence but does not silently rewrite it as evidence ages.

`--valid-from` answers “when was this true?” The event timestamp separately records “when did this
database learn it?” This is the distinction between valid time and transaction time.

The `--value` argument is parsed as JSON when possible:

```bash
# Int
--value 49568

# Bool
--value true

# Float
--value 0.73

# Probability uses the same JSON number syntax but additionally enforces 0 <= p <= 1
--value 0.73

# String (unquoted text that is not another JSON literal is treated as a string)
--value 'Enterprise research platform'

# An explicitly JSON-quoted String is equivalent
--value '"Enterprise research platform"'

# Enum or plain string
--value native

# Structured Json
--value '{"amount":602500,"currency":"USD","measure":"median"}'
```

Repeating an identical normalized assertion returns the original claim ID without adding another
event.

### Review agent output before publication

An agent can stage a claim that passes the same entity, type, and evidence validation as an
assertion but remains invisible to current projections:

```bash
epiq --actor agent:research propose-claim \
  --subject Barnstable \
  --question population \
  --value 49568 \
  --valid-from 2020-04-01 \
  --evidence evd_... \
  --rationale "Matches the cited decennial Census table"

epiq claim-proposals
epiq --actor human:reviewer review-claims prp_... \
  --decision approved --reason "Citation and interpretation verified"
```

`review-claims` accepts multiple proposal IDs. The entire selection is approved or rejected in one
transaction; a missing or previously reviewed proposal leaves every selected proposal unchanged.

Trusted agent pipelines can write a JSON array of claim objects directly. This is also one SQLite
transaction: if item 12 is malformed, items 0–11 do not leak into either events or projections.

```bash
epiq --actor agent:research bulk-assert --input claims.json
# Use --input - to read the JSON array from stdin.
```

### 7. Inspect the projection

```bash
epiq matrix --kind Town
```

The response contains question schemas, entity rows, projected cell states, values, confidence,
and lineage. A shortened cell looks like this:

```json
{
  "confidence": "high",
  "lineage": [
    {
      "claim_id": "clm_...",
      "evidence_id": "evd_...",
      "excerpt": "Barnstable population: 49,568...",
      "source": {
        "title": "2024 town estimates",
        "url": "https://api.example.gov/towns/barnstable"
      },
      "token": "p_clm_..."
    }
  ],
  "state": "Answered",
  "value": 49568,
  "values": [49568]
}
```

Select particular questions or historical cutoffs:

```bash
epiq matrix --kind Town --questions population,median_home_value
epiq matrix --kind Town --valid-at 2024-12-31
epiq matrix --kind Town --known-at 2026-01-01T00:00:00Z
```

At fixed valid- and transaction-time cutoffs, the projection is deterministic.

### 8. Record research that did not find an answer

Truro currently has two `Unasked` cells. Suppose an agent searches for a population source but
cannot establish an answer:

```bash
epiq --actor agent:census not-found \
  --subject "Truro" \
  --question population \
  --query 'Truro Massachusetts 2024 official population estimate' \
  --notes 'Checked the town profile and state portal; neither exposed a citable 2024 estimate.'
```

The Truro population cell becomes `NotFound`, while its home-value cell remains `Unasked`. This
records work performed without inventing a negative or zero value. A later supported assertion
will make the current cell `Answered`; the research task remains in the event history.

### 9. Understand contradictions and corrections

For a cardinality-one question, two active claims with different values produce `Contested`:

```bash
epiq assert --subject Barnstable --question population --value 49568 \
  --valid-from 2024-12-31 --evidence evd_first

epiq assert --subject Barnstable --question population --value 50000 \
  --valid-from 2024-12-31 --evidence evd_second
```

Epiq does not silently choose a winner. A reviewer resolves the conflict by retracting the claim
that should no longer be active:

```bash
epiq --actor human:reviewer retract clm_incorrect \
  --reason "The source rounded the ACS estimate; retain the exact table value."
```

Retraction closes the claim's transaction-time interval. It does not delete the assertion,
evidence, or original event. The current projection changes, while historical queries can still
recover what the database previously believed.

### Challenge a question that imposes the wrong categories

A source can reveal that the field itself is ill-typed rather than merely unanswered. For example,
`has_spinnaker: Bool` conflates a boat model's optional capability with the equipment installed on
an individual boat. Record that as a schema-level challenge:

```bash
epiq challenge-question has_spinnaker \
  --problem modal_ambiguity \
  --explanation "Can be equipped is different from currently has." \
  --example-entity "RS Quest" \
  --evidence evd_quest_options \
  --proposed-replacement '{
    "questions": [
      {"name":"spinnaker_availability","value_type":"Enum[standard,optional,unavailable,unknown]"},
      {"name":"spinnaker_equipped","value_type":"Bool","subject_kind":"Boat"}
    ]
  }'
```

This appends a `question.challenge` event and marks the projected question's `schema_state` as
`challenged`. It does not mutate the question, retract claims, or automatically apply the proposed
schema. Challenges are review-first:

```bash
epiq question-challenges --status open
epiq question-challenges --question has_spinnaker
epiq resolve-question-challenge qch_... --status resolved \
  --resolution "Created separate model capability and boat equipment questions."
```

The initial problem taxonomy is `type_mismatch`, `cardinality_mismatch`, `temporal_mismatch`,
`level_mismatch`, `population_mismatch`, `predicate_conflation`, `modal_ambiguity`,
`unit_mismatch`, `epistemic_mismatch`, `definition_ambiguity`, and `other`. Triggering evidence and
an example entity are optional but preserved when supplied.

### Retire or restore a field without erasing it

When a field is redundant, incorrectly typed, or asks the wrong question, remove it from current
tables with an append-only retirement:

```bash
epiq retire-question google_star_reviews \
  --reason "Google publishes an average rating, not the requested probability distribution."
```

This appends `question.retire`, removes the column from matrix, context, gaps, refresh-plan, export,
and agent-research projections, and rejects new claims against it. It does **not** delete the
field's definition, prior claims, evidence, challenges, or event history. The spreadsheet exposes
the same operation as the `×` control in each field's action row and requires a reason before
retiring it.

If the field becomes useful again, restore its original history and values:

```bash
epiq restore-question google_star_reviews \
  --reason "The field has been clarified and is needed again."
```

This appends `question.restore`; it does not manufacture a new field or duplicate the old claims.
In Epiq, this reversible retirement is what the UI means by “Remove field.” There is intentionally
no destructive schema-delete command.

### 10. Inspect the event history

```bash
epiq history
epiq history --type entity.create
epiq history --type claim.assert
epiq history --type claim.retracted
```

Each event includes a monotonically increasing sequence, event ID, timestamp, actor, type, and
payload. Use distinctive actors for research runs:

```bash
epiq --actor agent:census-refresh-2026-08 evidence ...
epiq --actor agent:census-refresh-2026-08 assert ...
```

`--actor` is a global option, so it appears before the subcommand.

### 11. Export without losing provenance

Create a self-contained interactive report:

```bash
epiq export-html --kind Town --output reports/towns.html
```

The generic explorer discovers questions from the database and displays:

- the entity-by-question matrix;
- automatic charts for numeric questions;
- coverage and unknown cells;
- evidence excerpts and source links;
- confidence and claim-token lineage.

Create a native Excel workbook:

```bash
epiq export-xlsx --kind Town --output reports/towns.xlsx
```

The workbook contains three sheets:

- `Data`: a conventional entity-by-question table.
- `Evidence`: one row per active claim lineage, including URLs and excerpts.
- `Unknowns`: `Unasked`, `NotFound`, and contested cells.

Both exporters are projections. The SQLite database remains the source of truth.

## Tutorial: preserve multiple forecasts as a distribution

Five forecasts are not one fact with a strangely shaped value. They are five separately sourced
claims that may disagree. Epiq preserves those observations first and derives an ensemble second.

Create an event and two questions:

```bash
epiq entity WeatherEvent "Boston rain on 2026-08-17" \
  --attributes '{"location":"Boston, MA","target_date":"2026-08-17"}'

epiq question rain_probability \
  --for WeatherEvent \
  --type Probability \
  --definition '{"label":"Provider rain probabilities","cardinality":"many"}'

epiq question forecast_distribution \
  --for WeatherEvent \
  --type 'Distribution[Float]' \
  --definition '{"label":"Forecast ensemble","cardinality":"one"}'
```

Each provider gets its own evidence and claim:

```bash
NOAA_EVIDENCE=$(epiq evidence \
  --url https://example.test/noaa/2026-08-17 \
  --title "NOAA forecast" \
  --retrieved-at 2026-08-16 \
  --excerpt "NOAA assigns a 40% chance of rain." | jq -r .evidence_id)

NOAA_CLAIM=$(epiq assert \
  --subject "Boston rain on 2026-08-17" \
  --question rain_probability \
  --value 0.40 \
  --valid-from 2026-08-17 \
  --evidence "$NOAA_EVIDENCE" | jq -r .claim_id)
```

Repeat that write for the other providers. Then derive an equally weighted empirical distribution
from the five claim IDs:

```bash
epiq --actor agent:weather-ensemble derive-distribution \
  --subject "Boston rain on 2026-08-17" \
  --question forecast_distribution \
  --input-claim "$NOAA_CLAIM" \
  --input-claim "$WEATHER_DOT_COM_CLAIM" \
  --input-claim "$ACCUWEATHER_CLAIM" \
  --input-claim "$APPLE_CLAIM" \
  --input-claim "$LOCAL_STATION_CLAIM" \
  --valid-from 2026-08-17
```

The projected value is:

```json
{
  "kind": "empirical",
  "samples": [0.4, 0.55, 0.35, 0.6, 0.45]
}
```

The derived claim additionally records:

- all five input claim IDs in order;
- all five inherited evidence fragments;
- the `empirical` derivation operation;
- its own actor, timestamp, confidence, and validity interval.

Use weights when providers should not contribute equally. Do this instead of the unweighted
derivation (or retract the earlier derived claim), otherwise both ensembles correctly appear as a
contested cardinality-one field:

```bash
epiq derive-distribution \
  --subject "Boston rain on 2026-08-17" \
  --question forecast_distribution \
  --input-claim "$NOAA_CLAIM,$WEATHER_DOT_COM_CLAIM,$ACCUWEATHER_CLAIM" \
  --weights '[0.5,0.3,0.2]' \
  --valid-from 2026-08-17
```

Weights must be finite, nonnegative, match the number of samples, and sum to one. A categorical
distribution supplied directly by a source uses a typed value such as:

```bash
epiq question rain_outcome \
  --for WeatherEvent \
  --type 'Distribution[Enum[rain,no_rain]]'

epiq assert \
  --subject "Boston rain on 2026-08-17" \
  --question rain_outcome \
  --value '{"kind":"categorical","probabilities":{"rain":0.4,"no_rain":0.6}}' \
  --valid-from 2026-08-17 \
  --evidence "$NOAA_EVIDENCE"
```

Categorical probabilities must cover exactly the declared outcomes and sum to one.

## What an agent loop looks like

A research agent does not need a privileged write path. Its loop is ordinary CLI composition:

1. Run `epiq matrix --kind Company` and locate `Unasked`, `NotFound`, stale, or contested cells.
2. Search externally using a browser, API, scraper, or another tool.
3. Add a bounded source excerpt with `epiq evidence`.
4. Submit the supported answer with `epiq assert`.
5. If a bounded search fails, record it with `epiq not-found`.
6. Regenerate HTML, Excel, or JSON projections.

All commands emit exactly one JSON value on success. Errors go to stderr in a machine-readable
shape:

```json
{
  "error": {
    "code": "entity_not_found",
    "message": "Entity not found: Barnstabel"
  }
}
```

Write commands return created IDs, so an agent can chain operations without parsing terminal prose.

## Python API

Bulk importers may use the same storage API directly:

```python
from epiq.store import Store

store = Store("research.sqlite")
store.initialize("Product landscape")

company_id = store.add_entity(
    "Company",
    "Example Research",
    {"domain": "example.test"},
    "import:seed",
)

store.add_question(
    "supports_sso",
    "Company",
    "Bool",
    {"label": "Supports SSO", "cardinality": "one"},
    "import:seed",
)

_, evidence_id = store.add_evidence(
    "https://example.test/security",
    "Security documentation",
    "2026-08-15",
    "Enterprise accounts support SAML SSO.",
    "agent:security-review",
)

store.assert_claim(
    company_id,
    "supports_sso",
    True,
    "2026-08-15",
    evidence_id,
    "agent:security-review",
)

matrix = store.matrix("Company")
```

The CLI is the preferred cross-package boundary. The Python API is useful for trusted importers.
Direct SQL reads are possible, but external code should not write tables directly because doing so
would bypass validation and the event log.

## Packaged examples

### Patriots and transaction time

The Patriots fixture demonstrates cells changing as results become known:

```bash
epiq --db /tmp/patriots.sqlite init --name "Patriots 2025"
epiq --db /tmp/patriots.sqlite demo patriots
epiq --db /tmp/patriots.sqlite season-record "New England Patriots 2025"
```

The final command returns `14-3` plus the 17 claim tokens used in the derivation. Move the
transaction-time cutoff backward:

```bash
epiq --db /tmp/patriots.sqlite season-record \
  "New England Patriots 2025" --known-at 2025-09-22T00:00:00Z
```

That returns `1-2`, because only three results were known at the cutoff.

### Cape Cod towns

This reproducible importer builds 15 towns, two questions, 15 evidence fragments, and 30 claims
from the Census ACS 2024 five-year release:

```bash
python scripts/build_cape_cod_towns.py --db examples/cape-cod-towns.sqlite
epiq --db examples/cape-cod-towns.sqlite export-html \
  --kind Town --output examples/cape-cod-towns.html
epiq --db examples/cape-cod-towns.sqlite export-xlsx \
  --kind Town --output examples/cape-cod-towns.xlsx
```

The visible questions are population and median owner-occupied home value. Each evidence excerpt
also retains the estimate's margin of error.

### Weather forecast distributions

The illustrative weather fixture creates five atomic provider forecasts and derives an empirical
distribution with full claim and evidence lineage:

```bash
python scripts/build_weather_forecasts.py --db examples/weather-forecasts.sqlite
epiq --db examples/weather-forecasts.sqlite matrix --kind WeatherEvent
epiq --db examples/weather-forecasts.sqlite export-html \
  --kind WeatherEvent --output examples/weather-forecasts.html
```

### Cham corpus adapter

`import-cham` translates the earlier entity/evidence/claim JSON packet into typed Epiq questions:

```bash
epiq use examples/ai-interviewers.sqlite
epiq init --name "AI Interviewer Market"
epiq --actor agent:corpus-import import-cham \
  --entities path/to/entities.json \
  --evidence path/to/evidence.json \
  --claims path/to/claims.json
epiq matrix --kind Company
```

The current adapter uses the primary evidence item for older multi-source claims. Native
many-to-many claim/evidence support is a planned storage migration.

## EpiQL v0.1

The repository also contains a deliberately narrow parser for a future research DSL:

```epiq
question game_result : Enum[W,L,T] for Game {
  ask "What was the final result?"
  cardinality one
}

derive wins : Int for Season =
  games |> where game_result == W |> count
```

Check a file without performing effects:

```bash
epiq --db /tmp/patriots.sqlite check examples/patriots.epiq
```

Unsupported expressions fail loudly. Planned language work includes populations, temporal lenses,
proposal-producing research effects, and explicit acceptance policies.

## Storage and concurrency

An Epiq project is one SQLite file. The principal tables are:

- `meta`
- `events`
- `entities`
- `questions`
- `sources`
- `evidence`
- `claims`
- `claim_evidence`
- `derivations`
- `claim_inputs`
- `research_tasks`

SQLite runs in WAL mode. Writes use `BEGIN IMMEDIATE`, which serializes competing writers rather
than allowing interleaved partial changes. Each accepted domain write and its event-log record are
committed in one transaction.

Current invariants:

1. Events, sources, and evidence are never updated or deleted.
2. A claim requires an existing evidence fragment.
3. Closing a claim changes its active interval; its assertion remains addressable.
4. Operational tables and replayable events change in one SQLite transaction.
5. A pure query at fixed valid- and transaction-time cutoffs is deterministic.
6. Failure to find evidence is not a negative claim.
7. Retried identical evidence and claim writes are idempotent.

The database is portable and can live beside a research project. Generated SQLite databases,
HTML reports, and Excel files are ignored in this repository by default.

## Spreadsheet web application

Epiq includes a functional local web application built with FastAPI, React, and TypeScript. The
spreadsheet is a projection of the same SQLite database used by the CLI; it does not maintain a
second source of truth.

The application currently supports:

- creating a workspace;
- adding entity rows and typed question columns on the fly;
- entering evidence-backed answers;
- inspecting confidence, excerpts, source links, and claim tokens in a cell drawer;
- displaying `Answered`, `Contested`, `NotFound`, and `Unasked` as distinct states;
- recording unsuccessful research without asserting a negative answer;
- retracting claims while preserving their event history;
- challenging incorrect answers, failed searches, and category/schema mistakes while preserving
  human guidance for subsequent agent runs;
- reversibly removing fields without deleting their historical claims or evidence;
- launching cell, row, column, and whole-table research with incremental progress indicators;
- finding independent supporting evidence without returning sources already attached to a claim;
- using AI to propose additional entity rows and typed fields, with checkbox-based human approval;
- assigning stable, slow-changing, or dynamic temporal policies and surfacing stale evidence;
- sorting any column, filtering rows by text or research status, and preserving view preferences;
- keyboard navigation with arrows/Tab, Enter or double-click inspection, and clipboard copy;
- frozen headers and identity columns, draggable column order, resizable columns, and compact or
  wrapped row density; and
- exporting the visible research domain to Excel or downloading a consistent project backup.

Single-click selects a cell without changing the sheet layout. Use the arrow keys or Tab to move,
Enter (or double-click) to open its evidence inspector, Escape to clear selection, and
Command/Ctrl+C to copy the displayed value. Pasting values is deliberately not yet a generic grid
operation: an Epiq answer needs evidence, confidence, and temporal context rather than an unsourced
scalar silently entering the database.

Install both development environments:

```bash
uv sync --extra test --extra web --extra web-test
npm --prefix web install
```

For frontend development, run the API and Vite development server in separate terminals:

```bash
EPIQ_DB=examples/my-market.sqlite uv run --extra web epiq-web
npm --prefix web run dev
```

Open `http://localhost:5173`. Vite proxies `/api` requests to FastAPI on port 8000. If the selected
database does not exist, the welcome screen initializes it.

For the single-server production-style build:

```bash
npm --prefix web run build
EPIQ_DB=examples/my-market.sqlite uv run --extra web epiq-web
```

Then open `http://127.0.0.1:8000`. FastAPI serves `web/dist` and falls back to `index.html` for
client-side routes. API documentation remains available at `http://127.0.0.1:8000/docs`.

The principal endpoints are:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/project` | Project identity and available tables |
| `POST` | `/api/project` | Initialize the selected SQLite file |
| `GET` | `/api/matrix/{kind}` | Current entity-by-question projection |
| `POST` | `/api/entities` | Add a row |
| `POST` | `/api/entities/{id}/aliases` | Add an alternate stable identity |
| `POST` | `/api/entities/{id}/merge` | Merge a duplicate into a surviving row |
| `POST` | `/api/entities/{id}/retire` | Retire a row without erasing it |
| `POST` | `/api/entities/{id}/restore` | Restore a retired row |
| `POST` | `/api/questions` | Add a typed, versioned column |
| `POST` | `/api/questions/{id}/retire` | Hide a field while preserving its history |
| `POST` | `/api/questions/{id}/restore` | Restore a retired field and its prior values |
| `POST` | `/api/questions/{id}/challenges` | Record a schema/category challenge |
| `GET` | `/api/question-challenges` | List and filter schema challenges |
| `POST` | `/api/question-challenges/{id}/resolve` | Resolve or dismiss a challenge |
| `POST` | `/api/evidence` | Add an immutable source excerpt |
| `POST` | `/api/claims` | Assert an evidence-backed cell answer |
| `POST` | `/api/claims/bulk` | Assert up to 1,000 claims atomically |
| `POST` | `/api/claim-proposals` | Stage a validated claim outside the live matrix |
| `GET` | `/api/claim-proposals` | Read the durable claim review queue |
| `POST` | `/api/claim-proposals/review` | Approve or reject a selection atomically |
| `POST` | `/api/claims/{id}/retract` | Close a claim without deleting it |
| `POST` | `/api/claims/{id}/supersede` | Atomically replace a claim |
| `POST` | `/api/research/not-found` | Record a completed unsuccessful search |
| `POST` | `/api/research/jobs` | Launch background cell or column research |
| `POST` | `/api/research/rows` | Research unanswered fields for one row |
| `POST` | `/api/research/table` | Research unanswered cells across the table |
| `POST` | `/api/entity-suggestions/jobs` | Propose additional rows for human review |
| `POST` | `/api/field-suggestions/jobs` | Propose additional typed fields for review |
| `GET` | `/api/export/{kind}.xlsx` | Download a provenance-aware Excel workbook |
| `GET` | `/api/export/project.sqlite` | Download a consistent project backup |
| `GET` | `/api/history` | Read the append-only event history |

### What a cell edit means

The UI deliberately does not implement silent replacement. Adding an answer creates a claim. If a
different active answer already exists in a single-valued field, the cell becomes `Contested` and
both claims remain inspectable. Retracting a claim closes its transaction-time interval but leaves
the original assertion and evidence in history.

## Operational safety and agent handoff

Before a long research run, create a transactionally consistent backup while the app is running:

```bash
epiq backup --output backups/market-before-refresh.sqlite
```

Existing files are never replaced unless `--force` is explicit. Check both SQLite integrity and
cross-table references with:

```bash
epiq doctor
```

Agents can orient themselves without reading this tutorial or guessing the schema:

```bash
epiq schema --kind Company
epiq context --kind Company --budget 4000
epiq gaps --kind Company
epiq stale --kind Company
epiq contradictions --kind Company
epiq refresh-plan --kind Company
epiq search "pricing announcement"
```

`context` returns current typed cells and confidence-aware lineage, compacting rows when the
approximate token budget would be exceeded. `gaps` distinguishes cells that have never been asked
from completed unsuccessful searches. `stale` follows each field's temporal policy rather than
decaying claim confidence.
`refresh-plan` turns those conditions into stable JSON tasks with typed questions, suggested search
queries, interpretation guidance, existing values, and source URLs for an external research agent.

Background research jobs and provisional entity/field suggestions are persisted in the project
database. Completed review queues therefore survive a server restart. A job interrupted by a stop
is marked failed at startup with an explicit retry message; Epiq never pretends it is still running.

Corrections that replace a claim can be committed atomically:

```bash
epiq supersede clm_OLD \
  --value '"closed"' \
  --valid-from 2026-08-01 \
  --evidence evd_NEW \
  --reason "Company closure announcement"
```

The old claim remains in history as `superseded`; either both changes commit or neither does.

## Current limits

Epiq is an executable vertical slice, not yet a production database server. In particular:

- the web server is intentionally loopback-oriented; there is no authentication or multi-tenancy,
  so it must not be exposed to an untrusted network;
- spreadsheet interactions do not yet include rectangular selection, copy/paste, formulas, or
  bulk fill;
- there are no web searches, scrapers, or LLM calls inside the CLI;
- question replacement migrations are not yet exposed as a full CLI workflow;
- EpiQL implements only question declarations and a narrow count-over-filter derivation;
- SQLite is canonical; there is not yet a separate append-only JSONL interchange format.

These boundaries are intentional enough to make experiments honest, but not promises that the
interface is finished. See [ROADMAP.md](ROADMAP.md) for the production sequence and release gates.

## Development

```bash
uv sync --extra test --extra web --extra web-test
uv run ruff check .
uv run pytest -q
npm --prefix web install
npm --prefix web run build
uv build
```

GitHub Actions runs lint and tests on every push and pull request.

## License

MIT
