# Git ledger architecture

**Status:** Accepted for implementation

**Tracking issue:** [#14](https://github.com/expectedparrot/epiq/issues/14)

## Decision

An Epiq project is a Git-versioned collection of immutable, canonical JSON transactions. The
transaction ledger is the only authoritative durable domain store. SQLite is a deterministic,
disposable projection used for queries, indexes, exports, and the web application.

Legacy SQLite conversion and a permanent dual-storage mode are not goals. Runtime state such as
active queues, locks, HTTP sessions, and progress messages is not part of the ledger. A durable
research outcome enters the ledger only when it becomes a domain transaction.

```text
Git ledger (authoritative)
        |
        | validate and reduce
        v
SQLite projection (rebuildable)
        |
        +-- CLI and Python reads
        +-- web API
        +-- search and reports
```

The architectural invariant is:

```bash
rm .epiq/projection.sqlite
epiq build
```

After rebuilding, every durable observable result must be equivalent: entities, questions,
evidence, claims, temporal history, provenance, proposals, reviews, derivations, and durable
research outcomes.

## Project layout

```text
epiq.toml
ledger/
  transactions/
    01/
      txn_01K....json
  schemas/
    transaction-1.schema.json
    operation-1.schema.json
artifacts/
  sha256/
    ab/
      abcdef...
.epiq/
  projection.sqlite
  projection.json
  write.lock
```

`ledger/transactions` and retained artifacts are committed. `.epiq` is ignored. Transaction files
are sharded by the first two characters after the `txn_` prefix to keep directories bounded.

## Domain transactions

Git commits are transport and review units. They are not Epiq transaction identities. An Epiq
transaction has its own stable ID and may share a Git commit with other transactions.

Every transaction contains:

- the format version and project ID;
- a globally unique transaction ID;
- the replica and actor that created it;
- a normalized UTC timestamp;
- zero or more causal parent transaction IDs;
- an optional caller idempotency key;
- one or more versioned domain operations; and
- a SHA-256 digest of the canonical transaction content excluding the digest member.

Every operation has a globally unique operation ID, a versioned operation type, optional causal
requirements, and a typed payload. Domain object IDs are allocated before the transaction is
written. Projection-local sequence numbers and SQLite row IDs never appear in the ledger contract.

## Canonical representation

Transactions use UTF-8 Epiq Canonical JSON 1 bytes for hashing: object keys are ordered
lexicographically by Unicode code point, insignificant whitespace is absent, non-ASCII text is
unescaped, and JSON numbers use Python's shortest round-trippable JSON representation. Input
parsers reject duplicate object keys, non-finite numbers, invalid Unicode, and timestamps that
cannot be normalized to an instant. Files end with one LF for ordinary text tooling; that LF is not
part of the canonical digest. This format is deliberately named and versioned so a future standards
change cannot silently alter existing transaction hashes.

The `digest` value is `sha256:<lowercase hex>` computed over Epiq Canonical JSON 1 for the document with its
top-level `digest` member omitted. A transaction ID collision with different canonical bytes is a
hard integrity error. Re-reading identical bytes is idempotent.

## Causality and deterministic reduction

`parents` names the transaction frontier observed by the writer. An empty parent list is valid only
for project genesis or transactions explicitly permitted to be causally independent.

The reducer validates the causal graph, then applies transactions in topological order. When two
ready transactions are concurrent, their transaction IDs provide a stable presentation order only.
That order must not silently resolve a domain conflict.

Reducer behavior cannot depend on:

- directory enumeration order;
- Git commit order;
- the local timezone or rebuild clock;
- SQLite autoincrement values; or
- Python container iteration order.

## Operation families

The initial operation catalog must cover all durable behavior exposed by Epiq:

| Family | Operations |
|---|---|
| Project | `project.initialize` |
| Entities | `entity_kind.define`, `entity.create`, `entity.alias`, `entity.merge`, `entity.retire`, `entity.restore` |
| Questions | `question.define`, `question.evolve`, `question.retire`, `question.restore`, `question.challenge`, `question.challenge_resolve` |
| Evidence | `evidence.capture`, `evidence.assess` |
| Claims | `claim.assert`, `claim.support`, `claim.supersede`, `claim.retract`, `claim.validity_end`, `claim.review` |
| Derivations | `derivation.declare` |
| Proposals | `proposal.create`, `proposal.review` |
| Research | `research.not_found`, `source.bundle_record` |
| Reports | `report.record` |

Operation payload schemas are versioned independently. Unknown optional metadata is preserved but
does not affect projection semantics. An unknown required operation type or version stops the build.

## Conflict semantics

Git text conflicts and Epiq domain conflicts are different. Unique immutable transaction filenames
make ordinary branch merges mostly additive. The reducer preserves domain disagreements.

Initial rules:

- Concurrent claim assertions survive as separate claims and may produce a contested cell.
- Supporting evidence is an add-only relation unless a later explicit assessment invalidates it.
- Retraction and supersession reference exact claim IDs; they never select a claim by arrival order.
- Concurrent retraction and support addition preserve both operations and expose the final support
  on the retracted claim's history.
- Compound entity identity collisions become an explicit identity conflict until a merge operation
  names the canonical entity.
- Entity merges form a directed canonicalization graph. Cycles are invalid.
- Concurrent question definitions do not receive versions based on arrival order. Their stable IDs
  remain distinct until an explicit evolution or resolution operation relates them.
- Competing final proposal reviews are an explicit review conflict. Last-write-wins is forbidden.
- Derivations reference exact input claim IDs and become stale when later operations change their
  applicability; inputs are never silently substituted.
- Missing parents, missing referenced objects, project mismatch, and digest mismatch are build
  errors rather than projected conflicts.

## Projection contract

`.epiq/projection.json` records:

- project ID;
- ledger format version;
- reducer implementation version;
- the applied transaction frontier;
- a digest of the applied transaction set; and
- the SQLite projection schema version.

Opening a project compares this metadata with the ledger. Missing transactions are applied
incrementally when the reducer version is compatible. Otherwise Epiq rebuilds. SQLite tables may be
updated freely by the reducer because they are materialized views, not authoritative history.

## Authoritative write protocol

A local writer:

1. acquires `.epiq/write.lock`;
2. refreshes the projection to the current ledger frontier;
3. validates and dry-runs the proposed transaction;
4. writes canonical bytes to a temporary file in the target directory;
5. flushes the file and atomically renames it to its final transaction path;
6. applies the transaction to SQLite and advances projection metadata; and
7. releases the lock.

The filesystem and SQLite cannot share an atomic commit. Therefore the ledger is committed first.
A crash after step 5 leaves SQLite behind; the next open detects and applies the transaction. An
acknowledged durable write must never exist only in SQLite.

## Git collaboration

Agents work on branches or worktrees and add uniquely named transaction files. Merging branches is
set union at the file layer. `epiq ledger merge-check` validates the combined causal graph and
reports both
integrity errors and newly visible domain conflicts.

Git is a transport and review protocol, not the database API. The CLI may invoke `git` as a
subprocess to clone a project, inspect or fetch a remote, and publish Epiq-owned files from the
current branch. Publishing validates and projects the ledger before staging files. It must never
stage unrelated workspace paths, silently integrate a divergent branch, force-push, or treat a Git
commit hash as a transaction ID. Pull-request creation is an optional review step implemented via
the GitHub `gh` CLI; the on-disk ledger format remains forge-independent.

Remote integration is deliberately two-phase:

1. fetch remote refs without changing the checked-out ledger;
2. explicitly merge or rebase using Git, then run `epiq merge-check` before publishing.

This boundary leaves textual conflict handling visible and ensures Epiq can diagnose the complete
merged transaction set before an agent advertises it as valid.

The expected offline CI sequence is:

```bash
epiq ledger validate
epiq ledger build --verify
epiq ledger doctor
pytest
```

## Artifacts and sensitive data

Evidence excerpts and metadata live in transaction payloads. Optional full captures are addressed
by SHA-256 and may be stored in Git, Git LFS, or an external content store. The ledger always keeps
the expected digest and retention mode.

Credentials, authorization headers, SQLite projections, WAL files, locks, and transient job
progress are never ledger content. Because Git retains history, redaction happens before a
transaction is written.

## Implementation sequence

1. Preserve and validate the current feature work.
2. Land the transaction and operation contracts.
3. Extract command validation from SQL projection mutation.
4. Implement deterministic full and incremental projection.
5. Route all durable writes through ledger transactions.
6. Add branch merge diagnostics and offline CI.
7. Benchmark and introduce snapshots or transaction packs only when needed.
8. Remove code paths that can create authoritative state only in SQLite.

## Acceptance criteria

- Deleting SQLite and rebuilding restores all durable observable state.
- Two clean machines produce equivalent projections from the same ledger.
- Random transaction discovery order produces the same result.
- Replaying or re-merging identical transactions is idempotent.
- Independent agent branches normally merge without textual conflicts.
- Concurrent epistemic disagreement remains visible.
- A crash can leave only the projection behind the ledger, never the reverse after acknowledgement.
- Project validation and projection rebuild require no network access.

