# Epiq

![Epiq — an agentic epistemic database](assets/epiq-hero.png)

Epiq is a local-first epistemic database for agent-driven research. It stores entities, typed
questions, source excerpts, and evidence-backed claims in SQLite. It can then project that history
into ordinary tables, interactive HTML, and Excel without throwing away where each cell came from.

Epiq's core database and CLI do not search the web or call a language model. The optional local web
application can launch replaceable research agents and submit their reviewed findings through the
same deterministic interface. This keeps research orchestration replaceable and makes storage
behavior testable.

This README builds a database from scratch before introducing the packaged examples.

## Run the web app locally — paste this into your coding agent

Copy the following prompt into Codex, Claude Code, or another terminal-capable coding agent. Replace
the project filename and display name if you already know what you want to research.

```text
Set up and launch the Epiq web application locally for me.

1. If you are already inside an Epiq checkout, use it. Otherwise clone
   https://github.com/expectedparrot/epiq.git and enter the repository.
2. Confirm Python 3.11+, uv, and Node.js are available. Do not replace or remove any existing
   databases, configuration, uncommitted work, or running research jobs.
3. Install the application and build the browser assets:
     uv sync --extra web
     npm --prefix web ci
     npm --prefix web run build
4. Create the project directory if needed, then select a persistent database for this checkout:
     mkdir -p .epiq/projects
     uv run epiq use .epiq/projects/my-research.sqlite
   Do not initialize or overwrite the file from the terminal. If it is new, let me name and create
   the project from the web welcome screen.
5. If OPENAI_API_KEY is already present in the environment, pass it through without printing,
   logging, or writing it into the repository. If it is absent, explain that the spreadsheet and
   manual evidence workflows will work but AI research buttons will require the variable.
6. Start the long-running local server from the repository root:
     uv run --extra web epiq-web
   Bind only to the default loopback address. If port 8000 is occupied by an existing healthy Epiq
   server, do not kill it until you have checked for active research jobs and confirmed whether it
   can be reused.
7. Verify that the server started, open http://127.0.0.1:8000 in my browser, and tell me:
   - the selected SQLite database path;
   - whether agent research is enabled;
   - the local URL; and
   - how to stop and restart the server safely.

Keep the server running after you finish. Do not expose it to the public network and do not commit
generated databases, workspace configuration, credentials, or build artifacts.
```

The selected database is remembered in `.epiq/config.json`, so subsequent launches only need:

```bash
uv run --extra web epiq-web
```

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

Observation and relation rows can declare a compound identity. Repeating the same kind and identity
returns the existing entity even if a caller proposes a different display name:

```bash
epiq entity Forecast --role observation \
  --identity '{"event":"rain_boston","forecaster":"Alice","issued_at":"2026-08-17T09:00:00Z"}'

epiq entity PriceQuote --role relation \
  --identity '{"product":"Acorn","plan":"Pro","region":"US","period":"monthly","effective":"2026-08-01"}'
```

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
- `URL`: an absolute `http://` or `https://` URL; the web application renders it as a safe,
  clickable new-tab link.
- `Date`: an ISO `YYYY-MM-DD` calendar date.
- `DateTime`: a timezone-aware ISO timestamp, including timestamps ending in `Z`.
- `Year`: an integer from 1 through 9999.
- `Interval[Date]`: `{"start":"YYYY-MM-DD","end":"YYYY-MM-DD"}`; `end` may be null.
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

Non-web evidence does not need a pretend URL. Its source type and deterministic URN are retained:

```bash
epiq evidence --type interview \
  --title "Technical interview notes" \
  --retrieved-at 2026-08-17 \
  --excerpt-file private-notes.md
```

Source types are `web`, `personal`, `model`, `report`, `interview`, and `other`.

Use structured locators for precise citations and optionally link the source to an entity already
modeled in Epiq:

```bash
epiq evidence --type report --title "Remote Work Study A" \
  --source-entity "Remote Work Study A" \
  --locator '{"page":12,"table":"3","section":"Results"}' \
  --retrieved-at 2026-08-17 --excerpt "The standardized effect was 0.18."
```

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

When newly found evidence supports one or more answers, `record` avoids manually copying evidence
IDs between commands while preserving the same underlying records:

```bash
epiq --actor agent:research record \
  --subject Barnstable \
  --source-type web \
  --url "https://api.example.gov/towns/barnstable" \
  --source-title "2024 town estimates" \
  --retrieved-at 2026-08-15 \
  --excerpt "Population 49,568; median home value $602,500." \
  --valid-from 2024-12-31 \
  --answer population 49568 \
  --answer median_home_value 602500
```

This is atomic syntactic sugar over `batch-write`: Epiq creates one evidence record and a separate
typed claim for each `--answer`. If any answer fails validation, none of the evidence or claims are
written. For one answer, use `--question population --value 49568` instead of `--answer`.

If one source supports cells on several rows, omit `--subject` and repeat
`--cell SUBJECT QUESTION VALUE`. Evidence and all cross-row claims still commit atomically:

```bash
epiq record --source-type report --source-title "Regional prices" \
  --retrieved-at 2026-08-17 --excerpt "Acorn: 10; Beacon: 20." \
  --valid-from 2026-08-17 \
  --cell Acorn price 10 \
  --cell Beacon price 20
```

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

When evidence and claims are both new, use `batch-write`. An `evidence.add` operation can define a
batch-local `ref`; later `claim.assert` operations consume it through `evidence_refs`:

```json
[
  {
    "op": "evidence.add",
    "ref": "funding_announcement",
    "url": "https://example.com/news",
    "title": "Funding announcement",
    "retrieved_at": "2026-08-17",
    "excerpt": "Acme has raised $12 million."
  },
  {
    "op": "claim.assert",
    "subject": "Acme",
    "question": "funding",
    "value": 12000000,
    "valid_from": "2026-08-17",
    "evidence_refs": ["funding_announcement"]
  }
]
```

Run it with `epiq --actor agent:research batch-write --input writeback.json`. A bad local reference,
invalid evidence, or invalid claim rolls back every evidence, source, event, and claim in the batch.
Each operation may provide its own `actor`; the event records that actor plus `submitted_by` when it
differs from the batch submitter. This lets an import agent preserve individual interviewers or
model runs as the originators of observations.

For repeatable setup, place `project`, `entity_kinds`, `entities`, `questions`, `aliases`, and
`operations` in one JSON object:

```bash
epiq --db project.sqlite apply --input project.json
epiq --db project.sqlite seed --input fixture.json
```

If the database is absent, `project.name` initializes it. Reapplying an unchanged declaration adds
no events; a changed question definition creates the next immutable version. Any conflict or bad
operation rolls the entire application back.

### Evolve a field without losing its history

A category error can be resolved as an executable schema transformation. For example, split one
ambiguous Boolean into two separately answerable fields:

```bash
epiq evolve-question has_spinnaker \
  --relationship splits \
  --reason "Capability and installed configuration are distinct" \
  --replacement '{"name":"spinnaker_available","value_type":"Enum[standard,optional,unavailable,unknown]"}' \
  --replacement '{"name":"spinnaker_installed","value_type":"Bool"}'

epiq question-lineage has_spinnaker
```

The successor definitions, lineage edges, and predecessor retirement commit together. Existing
claims remain attached to the old question version and therefore remain historically inspectable;
Epiq does not guess how to migrate semantically ambiguous answers.

### End a fact or challenge its evidence

Retraction means “we should no longer believe this assertion.” A validity end instead means “this
was true, and then stopped being true”:

```bash
epiq end-validity clm_... --valid-to 2025-01-01 --reason "Leadership changed"
```

This is bitemporal: a query with `--known-at` before the validity-end event still reconstructs the
earlier database belief, while `--valid-at 2025-06-01` after that event excludes the ended fact.

Evidence itself remains immutable, but its quality can be assessed repeatedly:

```bash
epiq assess-evidence evd_... --status disputed \
  --reason "The page may refer to a different company with the same name"
epiq evidence-assessments evd_...
```

Statuses are `accepted`, `disputed`, `invalid`, and `superseded`. The latest assessment is shown in
claim lineage; even invalid evidence is not erased, and Epiq does not silently retract every claim
that cites it.

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

Use compact terminal tables without changing the JSON-first default:

```bash
epiq --format table matrix --kind Town
epiq --format table query --kind Town --where 'population > 10000'
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

## Packaged CLI tutorials

Eleven narrated, executable tutorials live in [`examples/cli`](examples/cli/README.md). Four teach
the model with incremental CLI commands; seven stress-test forecasts, multidimensional pricing,
literature and clinical findings, procurement derivations, and recursive ownership/supply chains.
Build all eleven with:

```bash
uv run examples/cli/build-all.sh /tmp/epiq-cli-examples
```

Each tutorial is Markdown, every fixture enters through actual CLI commands, and the test suite
rebuilds all eleven projects from scratch and runs their integrity checks.

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
- adding multiple entity tables from the sidebar and switching between them;
- adding entity rows and typed question columns on the fly;
- defining one-to-one or one-to-many relationship fields that reference rows in another table;
- choosing related rows by name while retaining stable entity IDs underneath;
- researching many-valued relationships as a provisional graph: the agent proposes links, resolves
  existing related rows, identifies rows that must be created, and waits for checkbox approval;
- entering evidence-backed answers;
- inspecting confidence, excerpts, source links, and claim tokens in a cell drawer;
- displaying `Answered`, `Contested`, `NotFound`, and `Unasked` as distinct states;
- recording unsuccessful research without asserting a negative answer;
- retracting claims while preserving their event history;
- challenging incorrect answers, failed searches, and category/schema mistakes while preserving
  human guidance for subsequent agent runs;
- reversibly removing fields without deleting their historical claims or evidence;
- editing field labels, types, cardinality, time policy, and agent guidance through a compatibility
  preview before applying an immutable new schema version;
- launching cell, row, column, and whole-table research with incremental progress indicators;
- cancelling queued or running research without accepting late results, and retrying failed jobs;
- finding independent supporting evidence without returning sources already attached to a claim;
- describing a desired entity set in natural language (for example, “US Senators from the
  Northeast”), then reviewing sourced candidates as checkboxes before adding rows;
- using AI to propose additional typed fields, with checkbox-based human approval;
- assigning stable, slow-changing, or dynamic temporal policies and surfacing stale evidence;
- reviewing contradictions, stale evidence, and invalidated calculations from a unified queue;
- defining calculated fields, materializing them by field, and inspecting typed derivation lineage;
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
| `GET` | `/api/capabilities` | Versioned agent/tool protocol, optionally with live schema |
| `GET` | `/api/schema` | Current row types and typed fields |
| `GET` | `/api/context` | Token-budgeted current state for an agent |
| `GET` | `/api/matrix/{kind}` | Current entity-by-question projection |
| `GET` | `/api/gaps/{kind}` | Unanswered and unsuccessful research cells |
| `GET` | `/api/stale/{kind}` | Evidence made stale by field time policy |
| `GET` | `/api/contradictions/{kind}` | Contested cells and their lineage |
| `GET` | `/api/refresh-plan/{kind}` | Deterministic external-agent research tasks |
| `GET` | `/api/stale-derivations` | Derived claims with changed dependencies |
| `GET` | `/api/search` | Search identities, schema, evidence, and claims |
| `GET` | `/api/research/jobs` | Durable background-research activity |
| `POST` | `/api/research/jobs/{id}/cancel` | Cooperatively cancel work and discard late results |
| `POST` | `/api/research/jobs/{id}/retry` | Relaunch a failed or cancelled request |
| `POST` | `/api/entities` | Add a row |
| `POST` | `/api/apply` | Atomically converge a declarative project document |
| `POST` | `/api/entities/{id}/aliases` | Add an alternate stable identity |
| `POST` | `/api/questions/{id}/revision-preview` | Check a proposed field version against current values |
| `POST` | `/api/questions/{id}/revise` | Apply a compatible field revision with schema lineage |
| `POST` | `/api/entities/{id}/merge` | Merge a duplicate into a surviving row |
| `POST` | `/api/entities/{id}/retire` | Retire a row without erasing it |
| `POST` | `/api/entities/{id}/restore` | Restore a retired row |
| `POST` | `/api/questions` | Add a typed, versioned column |
| `POST` | `/api/questions/{id}/retire` | Hide a field while preserving its history |
| `POST` | `/api/questions/{id}/restore` | Restore a retired field and its prior values |
| `POST` | `/api/questions/{id}/evolve` | Atomically replace, refine, or split a field |
| `GET` | `/api/questions/{id}/lineage` | Read schema predecessor/successor lineage |
| `POST` | `/api/questions/{id}/challenges` | Record a schema/category challenge |
| `GET` | `/api/question-challenges` | List and filter schema challenges |
| `POST` | `/api/question-challenges/{id}/resolve` | Resolve or dismiss a challenge |
| `POST` | `/api/evidence` | Add an immutable source excerpt |
| `POST` | `/api/evidence/{id}/assess` | Append an evidence quality assessment |
| `GET` | `/api/evidence/{id}/assessments` | Read evidence assessment history |
| `POST` | `/api/claims` | Assert an evidence-backed cell answer |
| `POST` | `/api/claims/bulk` | Assert up to 1,000 claims atomically |
| `POST` | `/api/batch` | Atomically add evidence and dependent claims |
| `POST` | `/api/derive` | Persist a calculation with typed dependencies |
| `POST` | `/api/materialize` | Calculate declared formulas for ready rows |
| `POST` | `/api/propagate` | Materialize a claim through a relationship path |
| `POST` | `/api/aggregate/{kind}` | Group and summarize current numeric values |
| `POST` | `/api/claim-proposals` | Stage a validated claim outside the live matrix |
| `GET` | `/api/claim-proposals` | Read the durable claim review queue |
| `POST` | `/api/claim-proposals/review` | Approve or reject a selection atomically |
| `POST` | `/api/claims/{id}/retract` | Close a claim without deleting it |
| `POST` | `/api/claims/{id}/validity-end` | Record when a fact stopped being true |
| `POST` | `/api/claims/{id}/supersede` | Atomically replace a claim |
| `POST` | `/api/research/not-found` | Record a completed unsuccessful search |
| `POST` | `/api/research/jobs` | Launch background cell or column research |
| `POST` | `/api/research/rows` | Research unanswered fields for one row |
| `POST` | `/api/research/table` | Research unanswered cells across the table |
| `POST` | `/api/entity-suggestions/jobs` | Propose additional rows for human review |
| `POST` | `/api/field-suggestions/jobs` | Propose additional typed fields for review |
| `GET` | `/api/export/{kind}.xlsx` | Download a provenance-aware Excel workbook |
| `GET` | `/api/export/project.sqlite` | Download a consistent project backup |
| `GET` | `/api/export/project.epiq` | Download a checksummed portable project bundle |
| `POST` | `/api/query/{kind}` | Filter rows with structured predicates |
| `GET` | `/api/related/{entity}` | Traverse incoming or outgoing typed references |
| `GET` | `/api/reports/dossier/{entity}` | Read a sourced entity profile and history |
| `GET` | `/api/reports/timeline/{kind}/{question}` | Read a chronological field view |
| `POST` | `/api/reports/delta` | Record and read changes since a report baseline |
| `GET` | `/api/history` | Read the append-only event history |

### What a cell edit means

The UI deliberately does not implement silent replacement. Adding an answer creates a claim. If a
different active answer already exists in a single-valued field, the cell becomes `Contested` and
both claims remain inspectable. Retracting a claim closes its transaction-time interval but leaves
the original assertion and evidence in history.

## Querying, reports, and portable projects

`query` accepts concise expressions or JSON predicate objects. Predicates are combined with logical
AND and support `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, `contains_any`, `contains_all`,
`any_ref`, `in`, and `state`:

```bash
epiq query --kind Company \
  --where 'funding >= 10000000' \
  --where 'status=active'

epiq query --kind Work --where 'author=Paul Graham'
epiq query --kind Work --where 'topic contains_all ["Programming","Startups"]'
```

Reference predicates accept entity names and aliases. Matrix cells retain the stable ID in `value`
and add `{entity_id,name,kind}` objects in `display_value`/`display_values`. Traverse links and
backlinks directly:

```bash
epiq related "Paul Graham" --via author --direction incoming
epiq --format table related "Paul Graham" --direction incoming --depth 3
```

`--depth` performs bounded recursive traversal with cycle protection. Summarize numeric fields,
optionally grouping by another field:

```bash
epiq aggregate --kind PriceQuote --question price_usd --op avg --group-by region
epiq --format table aggregate --kind Forecast --question probability --op avg --group-by forecaster
```

`aggregate` is a read-only report. Use `derive` when the result should become a typed claim with
durable formula, input-claim, and inherited evidence lineage:

```bash
epiq --actor agent:ensemble derive \
  --subject "Rain tomorrow" --question ensemble_probability \
  --operation weighted_avg --parameters '{"weights":[1,2,1]}' \
  --valid-from 2026-08-18 \
  --input-cell "Alice forecast" probability \
  --input-cell "Bob forecast" probability \
  --input-cell "Carol forecast" probability
```

Operations are `sum`, `avg`, `min`, `max`, `count`, `weighted_avg`, and `linear`. `linear` accepts
`{"scale":...,"offset":...}` for conversions such as annual to monthly price. Inputs may also be
provided directly by repeating `--input-claim`.

Weights can themselves be sourced claims. Repeat `--weight-cell SUBJECT QUESTION` in the same
order as the weighted inputs; Epiq inherits their evidence and records their claim IDs:

```bash
epiq derive --subject "Sleep review" --question pooled_effect \
  --operation weighted_avg --valid-from 2026-08-17 \
  --input-cell "Study A finding" effect --input-cell "Study B finding" effect \
  --weight-cell "Study A finding" sample_size --weight-cell "Study B finding" sample_size
```

For a formula shared by a table, declare it in the target question's definition and materialize all
ready rows together. Rows missing an input are reported as skipped:

```bash
epiq question landed_cost --for Quote --type 'Quantity[USD]' \
  --definition '{"formula":{"operation":"sum","inputs":["price","shipping"]}}'
epiq materialize --kind Quote --valid-from 2026-08-17
```

Relationship traversal can also produce a derived claim. `propagate` selects the nearest related
entity with the requested source claim and rejects ambiguous equally-near matches:

```bash
epiq propagate --subject Acorn --via parent_company --direction outgoing --depth 5 \
  --question risk_level --to-question inherited_risk --valid-from 2026-08-17
```

Omit `--via` when a path intentionally crosses differently named reference fields, such as
`product.component → component.subcomponent → component.supplier`.

Every derived claim has typed `operand`, `parameter`, and `path` dependencies. Check whether an
input has been retracted, superseded, or followed by a newer active claim:

```bash
epiq stale-derivations
epiq stale-derivations --kind Company
```

Staleness is reported rather than silently recomputed: the original derivation remains an auditable
historical assertion, while an agent or human can inspect the change and rematerialize it.

For scripts, suppress successful output, select one JSON path, or request collected IDs:

```bash
epiq --quiet apply --input project.json
epiq --select query.matched query --kind Company --where 'stage=seed'
epiq --format ids entity Company "Acme"
```

The same valid-time and transaction-time controls as `matrix` are available through `--valid-at`
and `--known-at`. Reports are deterministic JSON rather than prose invented by the CLI:

```bash
epiq dossier "Acme"
epiq timeline --kind Company --question funding
epiq delta                       # changes since the prior delta report
epiq delta --since-seq 120       # explicit event baseline
```

Each delta records a `report.generated` event containing its through-sequence and content hash, so
the next delta has a durable baseline.

For transfer or archival, export a `.epiq` bundle rather than copying a live WAL database:

```bash
epiq export-bundle --output backups/market.epiq
epiq --db restored/market.sqlite import-bundle backups/market.epiq
```

The bundle contains an online SQLite backup and a versioned manifest with byte length and SHA-256
checksum. Import refuses unexpected files, checksum mismatches, corrupt SQLite, and existing
destinations.

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

Inspect upgrades before applying them, and take a transactionally consistent snapshot of the old
schema as part of an explicit migration:

```bash
epiq migration-plan
epiq migrate --backup backups/market-before-v10.sqlite
```

Epiq refuses databases created by a newer unsupported schema version. SQLite triggers prevent
updates or deletions of events, evidence fragments, and sources even if an application path is
implemented incorrectly. `doctor` additionally checks materialized entities, questions, evidence,
and claims against their originating event types and verifies every claim's primary evidence link.

Agents can orient themselves without reading this tutorial or guessing the schema:

```bash
epiq capabilities
epiq capabilities --command record
epiq capabilities --include-schema
epiq schema --kind Company
epiq context --kind Company --budget 4000
epiq gaps --kind Company
epiq stale --kind Company
epiq contradictions --kind Company
epiq refresh-plan --kind Company
epiq search "pricing announcement"
```

`capabilities` does not require an initialized database. It returns a versioned protocol declaration
with every command's arguments, constraints, mutation and transaction behavior, return shape,
examples, supported types and operations, JSON document shapes, common errors, and recommended
agent workflows. `--command` narrows the response for a token-efficient tool lookup;
`--include-schema` combines protocol discovery with the selected project's current schema.

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
- spreadsheet interactions do not yet include rectangular selection, paste, or bulk fill; formula
  fields support row-level numeric operations but not arbitrary spreadsheet expressions;
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
