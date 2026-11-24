# E[🦜]

Epiq is a local-first epistemic database for agent-driven research. Its authoritative datastore is
a Git-versioned directory of immutable JSON transactions containing entities, typed questions,
source excerpts, and evidence-backed claims. SQLite under `.epiq/` is a disposable projection for
queries and interfaces; it is never the source of truth.

Epiq separates durable research state from replaceable orchestration. The CLI validates every
write and emits machine-readable JSON. The optional local web application adds a spreadsheet,
document pipelines, and a persistent project-scoped Control Center agent.

## Install the CLI

Epiq requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/expectedparrot/epiq.git
epiq install-status
```

Upgrade an existing installation before starting important work:

```bash
uv tool upgrade epiq
epiq install-status
```

`install-status` reports the installed version and path, Python compatibility, bundled frontend,
optional modules, available `git`/`pi` commands, and whether research-provider credentials are
configured. It reports only credential presence, never credential values.

## Coding-agent quickstart — click to copy

Paste this prompt into Codex, Claude Code, or another terminal-capable coding agent:

```text
Use Epiq's CLI to build or maintain an evidence-backed research project. Epiq's authoritative
datastore is a Git-versioned directory of immutable JSON transactions. Never edit ledger
transactions or `.epiq/projection.sqlite` directly, and never commit `.epiq/` or credentials.

Before working, install or update Epiq and inspect its machine-readable guidance:
  uv tool install git+https://github.com/expectedparrot/epiq.git
or, if it is already installed:
  uv tool upgrade epiq
then run:
  epiq install-status
  epiq capabilities
  epiq guide workflow
  epiq guide schema-design
  epiq guide schema-patterns
  epiq guide evidence
  epiq guide git
  epiq guide recovery

If working inside an Epiq source checkout, do not install over it. Run `git pull --ff-only`,
`uv sync --extra test`, and use `uv run epiq`.

Commands return one versioned JSON envelope. Read results from `data`, failures from `errors`, and
review `next_steps`. Run `epiq capabilities --command COMMAND` before guessing arguments. Select a
project with `--project DIRECTORY`, `EPIQ_PROJECT`, `epiq use-project DIRECTORY`, or by entering a
directory containing `epiq.toml`.

Design the collection before scaling it. Define inclusion rules and stable identities; use
Ref[Kind] relationships instead of embedded names; model repeated events as rows; prefer atomic
numeric, enum, boolean, date, year, quantity, URL, and image fields over long prose; and propose a
minimal core plus useful adjacent tables. Pilot typical, missing, ambiguous, and changing cases.

Use IDs returned by Epiq and stable `--idempotency-key` values for automated writes. Record claims
with real bounded excerpts, retrieval dates, and validity dates. Use `not-found` for an unsuccessful
search, never as evidence that an answer is negative. Never invent evidence.

After writes run `epiq doctor`. After Git integration run `epiq merge-check`, inspect
`epiq conflicts`, and run `epiq doctor`. Publish only Epiq-owned paths with
`epiq remote publish --message MESSAGE`; use `--pr --base main` when review is appropriate.
```

## Five-minute CLI workflow

Create a project and inspect the built-in orientation surface:

```bash
epiq --project research init --name "Market research"
epiq --project research status
epiq --project research guide 2>/dev/null || epiq guide workflow
```

Create a typed table, one row, and one field:

```bash
epiq --project research kind Company
epiq --project research entity Company Acme --idempotency-key entity-acme
epiq --project research question employees --for Company --type Int \
  --idempotency-key question-company-employees
```

Record evidence and its supported answer atomically:

```bash
epiq --project research record \
  --subject Acme --question employees --value 12 \
  --source-title "Acme company profile" --source-type web \
  --url https://example.test/acme --retrieved-at 2026-11-23 \
  --excerpt "Acme has 12 employees." --valid-from 2026-11-23 \
  --idempotency-key acme-employees-2026-11-23

epiq --project research matrix --kind Company
epiq --project research doctor
```

Every response is a JSON envelope. Add `--human` for prose, `--format table` for compact tabular
output, `--select PATH` to extract one JSON value, or `--select PATH --raw` to emit a scalar for
shell composition.

## Executable smoke test with returned IDs

This creates an isolated project and chains identifiers without parsing prose or using placeholders:

```bash
PROJECT="$(mktemp -d)/epiq-smoke"
epiq --project "$PROJECT" init --name "Epiq smoke test"
epiq --project "$PROJECT" kind Company
ENTITY_ID="$(epiq --project "$PROJECT" --select entity_id --raw entity Company Acme \
  --idempotency-key smoke-entity)"
QUESTION_ID="$(epiq --project "$PROJECT" --select question_id --raw question employees \
  --for Company --type Int --idempotency-key smoke-question)"
epiq --project "$PROJECT" record \
  --subject-id "$ENTITY_ID" --question-id "$QUESTION_ID" --value 12 \
  --source-title "Smoke-test fixture" --source-type report \
  --url https://example.test/acme --retrieved-at 2026-11-23 \
  --excerpt "Acme has 12 employees." --valid-from 2026-11-23 \
  --idempotency-key smoke-record
epiq --project "$PROJECT" matrix --kind Company
epiq --project "$PROJECT" doctor
```

## Design a useful collection

Do not translate the nouns in a request into one flat table. Start with:

```bash
epiq guide schema-design
epiq guide schema-patterns
epiq schema-plan faculty
epiq schema-plan literature
epiq schema-plan startups
```

A useful schema normally distinguishes:

- stable things with independent identity, such as people, organizations, papers, products, and
  places;
- repeated events or observations, such as appointments, funding rounds, earnings calls, citations,
  and news coverage;
- typed relationships using `Ref[Kind]`;
- analysis-ready fields such as numbers, quantities, years, dates, enums, booleans, URLs, and images;
- explicit scope, inclusion rules, identifiers, evidence requirements, and time policy.

For example, a faculty project may begin with `Faculty` and `AcademicGroup`, then add `Paper`,
`Authorship`, `Course`, `Appointment`, and `NewsArticle` when those tables answer useful questions.
Pilot the schema before collecting hundreds of rows.

## Existing projects and Git collaboration

Clone or select an existing project:

```bash
epiq remote clone git@github.com:example/research.git --path research
epiq use-project research
epiq status
```

Fetch never silently merges. Integrate explicitly, then validate semantic and structural state:

```bash
epiq remote fetch
git merge origin/main
epiq merge-check
epiq conflicts
epiq doctor
```

Publish only Epiq-owned files (`epiq.toml`, `.gitignore`, `ledger/`, and retained `artifacts/`):

```bash
epiq remote publish --message "Add competitor evidence"
epiq remote publish --message "Add pricing review" --pr --base main
```

## Long documents and extraction pipelines

Keep PDFs and transcripts as content-addressed artifacts rather than pasting entire documents into
agent context or database cells. Epiq prepares deterministic bounded chunks and accepts only
findings whose supporting quote occurs in the selected chunk:

```bash
epiq --project research document-add --input call.pdf \
  --title "Q3 earnings call" --type earnings-call-transcript --retention git-lfs
epiq --project research pipeline-define --input transcript-facts.json
epiq --project research pipeline-prepare \
  --document rpt_DOCUMENT --pipeline rpt_PIPELINE
epiq --project research pipeline-extract --run rpt_RUN --workers 4
```

Use `epiq capabilities --command document-add` and the corresponding pipeline commands for exact
contracts. Pipeline jobs write verified results incrementally; the web application checkpoints
completed chunks and resumes unfinished work after a restart.

## Exports

```bash
epiq --project research export --format matrix-csv --kind Company \
  --output companies.csv
epiq --project research export --format claims-jsonl --kind Company \
  --output company-claims.jsonl
epiq --project research export --format scenario-list --kind Company \
  --output companies.scenario-list.ep
epiq --project research export --format agent-list --kind Company \
  --output companies.agent-list.ep --record-manifest
epiq --project research export --format html --kind Company --output companies.html
```

EDSL exports are native `.ep` packages. Install the `edsl` optional dependency when using them.

## Run the web application

For development or the current web interface, use a source checkout:

```bash
git clone https://github.com/expectedparrot/epiq.git
cd epiq
uv sync --extra web
npm --prefix web ci
npm --prefix web run build
export EPIQ_PROJECT="$PWD/.epiq/projects/my-research"
mkdir -p "$(dirname "$EPIQ_PROJECT")"
uv run --extra web epiq-web
```

Open <http://127.0.0.1:8000>. Keep the project path stable across restarts. The browser can create a
new project at that path.

Research integrations are optional. `epiq install-status` shows readiness. The default direct
reasoner uses `OPENAI_API_KEY`; Firecrawl retrieval additionally uses `EPIQ_SOURCE_PROVIDER=firecrawl`
and `FIRECRAWL_API_KEY`; the persistent Control Center uses the configurable `pi` executable. Never
print, log, or commit credential values. Bind the local application only to loopback unless you have
added an appropriate authentication and deployment layer.

## Develop Epiq

```bash
git clone https://github.com/expectedparrot/epiq.git
cd epiq
uv sync --extra test --extra web
npm --prefix web ci
uv run pytest -q
uv run ruff check .
npm --prefix web run build
```

Inside a source checkout, prefer `uv run epiq` and `uv run epiq-web` so you test the checked-out
code rather than a separately installed executable.

## Agent protocol and recovery

These commands do not require guessing flags or reading this README:

```bash
epiq install-status
epiq capabilities
epiq capabilities --command record
epiq guide workflow
epiq guide evidence
epiq guide git
epiq guide recovery
epiq agent schema envelope
epiq docs list
```

Success and failure use the same versioned envelope shape. Errors are written to stderr with exit
status 2. Suggested next actions are tokenized argv arrays and declare mutation, network, and
approval requirements.

The Git-ledger architecture is documented in
[`docs/git-ledger-architecture.md`](docs/git-ledger-architecture.md). Source-provider contracts and
provenance are documented in
[`docs/source-provider-protocol-spec.md`](docs/source-provider-protocol-spec.md) and
[`docs/source-providers-and-provenance.md`](docs/source-providers-and-provenance.md).

Legacy SQLite-only commands remain available only through explicit `--db` compatibility workflows.
They are not the architecture or onboarding path for new projects.
