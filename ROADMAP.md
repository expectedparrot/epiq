# Epiq production roadmap

This roadmap translates the original `BRIEF.md`/`SPEC.md` into the current SQLite-based Epiq
architecture. The SQLite database is the portable source of truth: domain changes append events,
while replaceable operational state (such as agent-job progress) lives in explicitly non-ledger
tables.

## Production foundation — implemented

- Transactional, append-only events for entities, questions, evidence, claims, challenges,
  derivations, and research outcomes.
- WAL mode, immediate write transactions, foreign keys, busy timeouts, and concurrent-writer tests.
- Strict scalar, enum, probability, and probability-distribution validation.
- Evidence-required claims, multiple sources per claim, idempotent evidence linking, temporal policy,
  NotFound records, contested cells, and human challenge/research-guidance workflows.
- Atomic claim supersede: replacement assertion and closure of the prior claim commit together.
- Reversible field retirement: `question.retire` removes an unsuitable column from current
  projections and agent work without deleting its schema, claims, evidence, or history;
  `question.restore` brings it back.
- Schema-versioned automatic migrations (currently v9).
- Stable entity identity with normalized duplicate rejection, aliases, merge redirects, and
  reversible entity retirement. Historical claims keep their original subject IDs while current
  projections unify merged identities.
- Typed relationships (`Ref[EntityKind]`) and unit-bearing numeric measurements (`Quantity[unit]`).
- Durable, validated claim proposals with atomic multi-selection approval/rejection, plus atomic
  direct claim batches that roll back every event and projection when any item fails.
- Executable schema evolution for replacing, refining, or splitting fields atomically, with typed
  successor definitions, predecessor/successor lineage, and reversible old-field retirement.
- Durable agent jobs and proposals. Completed jobs survive restarts; interrupted jobs become explicit
  failures rather than remaining permanently “running.”
- Consistent online SQLite backups and a `doctor` integrity command.
- Agent orientation and planning surfaces: `schema`, `context`, `gaps`, and `stale`.
- Deterministic `refresh-plan` generation from gaps, staleness, and contested cells, plus a sourced
  `contradictions` view.
- Project creation/open/close, Excel export, typed spreadsheet UI, incremental research status, and
  provisional AI suggestions for rows and fields.

## P1 — hardening for trusted local deployment

- Replace implicit migrations in `connect()` with a migration registry, migration lock, dry-run
  inspection, and backup-before-major-migration policy.
- Persist an explicit job request envelope sufficient to retry interrupted work safely, with retry,
  cancellation, and bounded retention controls.
- Add canonical URL normalization and stronger evidence idempotency across tracking URLs.
- Add explicit idempotency keys to every write API and CLI command.
- Add atomic project import/export containing the database, retained captures, and a manifest with
  checksums.
- Add structured application logging with secret redaction and request/job correlation IDs.
- Add configurable API authentication and origin policy before any non-loopback deployment.

## P2 — agent/database ergonomics

- Search over entity names, question definitions, claim values, evidence titles, and excerpts using
  SQLite FTS5.
- Deterministic contradiction reports, entity dossiers, timelines, and change-since-baseline reports.
- Rich duplicate suggestions and interactive merge review on top of the implemented alias/merge
  primitives.
- A schema evolution assistant UI that turns challenge proposals into the implemented evolution
  operation.

## P3 — spreadsheet interaction

- Rectangular selection, copy/paste, keyboard navigation, undo as compensating events, filtering,
  frozen columns, and saved views.
- Spreadsheet review surfaces for the implemented claim queue and existing schema proposals.
- Formula/derivation builder backed by a broader typed EpiQL rather than opaque spreadsheet formulas.
- Resumable live job event stream (SSE or WebSocket) with polling as fallback.

## Release gates

A `1.0` release should require:

1. No known path that mutates or deletes epistemic history.
2. Recovery tests for process interruption during every write class and background-job phase.
3. Migration fixtures for every supported historical schema version.
4. Backup/restore and project-export round trips tested on realistic databases.
5. Warm projection and search benchmarks at 10,000, 100,000, and 1,000,000 events.
6. Threat model and authentication enabled for any network-accessible mode.
7. Stable JSON schemas for CLI/API inputs, outputs, and errors with compatibility tests.
