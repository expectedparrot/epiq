# Historical imports and atomic proposal bundles

These APIs are part of the supported `epiq.Database` Python interface. They address
GitHub issues #10 and #11; timestamp handling also addresses #13.

## Dates and timestamps

Ledger timestamps and read cutoffs accept `YYYY-MM-DD` or a timezone-aware ISO
value such as `2018-01-13T08:07:00-10:00`. A date alone means **midnight UTC at the
start of that day**, including for `known_at` and `valid_at`. To include a whole
day, supply its final instant, for example `2018-01-13T23:59:59.999999Z`.

New datetime metadata is stored in UTC with six fractional digits. Precision
beyond microseconds, naive datetimes, invalid offsets, and malformed values raise
`EpiqError` with code `invalid_timestamp`. Calendar dates retain their date-only
representation. Interval starts are inclusive; validity endings and retractions
exclude the ending instant. Retractions change transaction time; validity endings
change valid time and take effect only once the ending event is known.

Existing timestamp strings are compared chronologically using the same parser.
No migration rewrites immutable events, evidence, or claim IDs. Equivalent old
and new representations also match when retrying assertions. Invalid legacy
metadata is reported by `doctor`; affected projections raise a domain error.

## Complete historical phases

Start with an empty project and import phases in chronological order. Each phase
is one transaction, including entity/schema creation, evidence, assertions,
retractions, research tasks, the import audit event, and its retry receipt.

```python
import epiq

db = epiq.open("history.sqlite", create=True)
document = {
    "entities": [{"kind": "Event", "name": "Example"}],
    "questions": [
        {"name": "confirmed", "subject_kind": "Event", "value_type": "Bool"}
    ],
    "operations": [
        {
            "op": "evidence.add", "ref": "source",
            "url": "https://example.test/archive", "title": "Archive",
            "retrieved_at": "2018-01-13", "excerpt": "The event was confirmed.",
        },
        {
            "op": "claim.assert", "subject": "Example", "question": "confirmed",
            "value": True, "valid_from": "2018-01-13", "evidence_refs": ["source"],
        },
    ],
}
db.import_document(
    document, recorded_at="2018-01-13T08:07:00-10:00",
    actor="import:archive", idempotency_key="archive-phase-1",
)
snapshot = db.matrix("Event", known_at="2018-01-14")
```

All new ledger events in this phase have transaction time
`2018-01-13T18:07:00.000000Z`. Existing objects retain their original creation time.
The project file's creation metadata remains the time the file was initialized.
Importing a new phase before existing ledger events raises `invalid_chronology`;
a failed phase leaves no partial objects or retry receipt. An exact retry of an
older successful phase returns its original result, even after later phases.

Backdating only `Store.assert_claim(recorded_at=...)` is the legacy claim-only
mechanism; it cannot create a complete historical snapshot. Use `import_document`
for imports. For fixtures, `epiq.open(..., clock=lambda: "2018-01-13T23:00:00Z")`
also controls initialization and ordinary ledger writes. A fixture clock must not
move backwards relative to existing events. These are explicit caller-supplied
transaction times, intended for trusted importers and fixture construction.

## Stage a whole document

The same document can enter a review queue before any of its facts become live:

```python
proposal = db.propose(document, actor="agent:research", idempotency_key="research-run-1")
queue = db.proposals()
result = db.review_proposal(
    proposal, "approved", "Checked against the archive", actor="human:reviewer"
)
```

Staging executes the normal validation path inside a savepoint and discards all
candidate writes before saving the proposal. Even new entities and questions stay
out of live matrices until acceptance. Approval revalidates against current data
and commits the complete document plus its review event atomically. A bad later
candidate rolls back earlier writes. Rejection writes only the decision.

`db.proposals(status=None)` includes decided proposals, the original document,
proposer, timestamp, decision, reason, and resulting IDs. Accepted writes retain
normal event attribution; the proposal preserves the submitting actor and request.
This API stages whole bundles; it does not yet provide editable proposal revisions
or a dedicated web review interface for these bundles.

Documents and ordinary batch writes also accept these operations:

```python
operations = [
    {"op": "claim.retract", "claim_id": "clm_...", "reason": "Source corrected"},
    {
        "op": "research.not_found", "subject": "Example", "question": "confirmed",
        "query": "Archive search bounded to January 2018", "notes": "No support found",
        "idempotency_key": "archive-search-run-2",
    },
]
```

## Retry policy

- Keys are scoped to the operation and submitting actor. Concurrent identical
  retries return one durable result. Reusing a key with different content raises
  `idempotency_conflict`.
- Without an explicit key, the canonical JSON request supplies a deterministic
  key. Property order does not matter; array order and supplied defaults do.
- Approved and rejected proposals remain final. Retrying their original request
  returns the original proposal; repeating the same review decision returns its
  original result. Submit a new key to start another proposal after rejection.
- `Store.propose_claim` and `Store.record_not_found` also accept an optional
  `idempotency_key`. A genuinely new search should use a new run key even if its
  query and notes match an earlier search. Web research jobs supply their own
  run-scoped keys, preserving independent research attempts.

Schema version 14 adds proposal and retry tables in an atomic migration. Existing
facts and event payloads are preserved. Older Epiq versions cannot open a database
after this upgrade; use `epiq migrate --backup ...` when retaining an older copy.
