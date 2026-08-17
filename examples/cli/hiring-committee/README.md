# Tutorial: preserve hiring-committee evidence and disagreement

This example treats candidates as rows and evaluation criteria as typed columns. Its key lesson is
that two reviewers' assessments should not be flattened into one unexplained cell.

> Hiring is consequential. This synthetic tutorial demonstrates provenance and disagreement, not
> an automated hiring policy. Do not collect protected traits, and keep decisions under appropriate
> human and legal review.

## 1. Create the project and its rows

```bash
epiq use /tmp/hiring.sqlite
epiq init --name "Hiring committee tutorial"
epiq entity Candidate "Alex Rivera"
epiq entity Candidate "Morgan Lee"
epiq entity Role "Product Engineer"
epiq entity Role "Research Engineer"
```

`epiq use` selects this file for the workspace, so the rest of the tutorial does not need to repeat
`--db`. Use `epiq db` to check the active project.

Candidates and roles are separate entity kinds. That lets a recommendation refer to a real role
rather than copy a name that may later change.

## 2. Define columns according to their meaning

```bash
epiq question technical_strength --for Candidate \
  --type String \
  --definition '{"label":"Technical strength","cardinality":"many"}'

epiq question interviewer_rating --for Candidate \
  --type Probability \
  --definition '{"label":"Interviewer confidence","cardinality":"many"}'

epiq question recommended_role --for Candidate \
  --type 'Ref[Role]' \
  --definition '{"label":"Recommended role","cardinality":"many"}'

epiq question committee_recommendation --for Candidate \
  --type 'Enum[hire,no_hire,hold]' \
  --definition '{"label":"Committee recommendation","cardinality":"one"}'
```

The first three are many-valued because reviewers can independently provide legitimate answers.
The committee outcome is single-valued because it represents the final institutional decision.

### What did those commands build?

Run the matrix projection:

```bash
epiq matrix --kind Candidate
```

Conceptually, the database now projects to this table:

| Candidate | Technical strength (`String`, many) | Interviewer confidence (`Probability`, many) | Recommended role (`Ref[Role]`, many) | Committee recommendation (`Enum`, one) |
| --- | --- | ---: | --- | --- |
| Alex Rivera | Unasked | Unasked | Unasked | Unasked |
| Morgan Lee | Unasked | Unasked | Unasked | Unasked |

The two `Candidate` entities became rows. The four questions whose `--for` value is `Candidate`
became columns. `Product Engineer` does **not** appear as a row here: it belongs to the separate
`Role` entity kind and can be referenced from cells in the Recommended role column.

`Unasked` is a real state, not an empty string or a false answer. It means no answer and no
completed unsuccessful search have been recorded for that candidate and question.

## 3. Record private evidence and its supported answers

There is no public URL, and Epiq does not require one. `record` accepts the evidence and all the
answers supported by it as one atomic operation:

```bash
epiq --actor interviewer:maya record \
  --subject "Alex Rivera" \
  --source-type interview \
  --source-title "Alex Rivera technical interview" \
  --retrieved-at 2026-08-17 \
  --excerpt "Alex decomposed the queueing problem clearly. Confidence: 0.82. Recommended for Product Engineer." \
  --valid-from 2026-08-17 \
  --answer technical_strength "Clear decomposition of queueing problems" \
  --answer interviewer_rating 0.82 \
  --answer recommended_role "Product Engineer"
```

The source type says how this information was obtained. The actor says who introduced it into the
ledger. A production system can point the source locator at a private document without pretending
it is a public webpage.

The result identifies everything that was created:

```json
{
  "answer_count": 3,
  "claim_ids": ["clm_...", "clm_...", "clm_..."],
  "evidence_id": "evd_...",
  "ok": true,
  "source_id": "src_..."
}
```

Internally, Epiq still creates one source, one evidence fragment, and three separately typed claims.
If any answer is invalid, the entire operation rolls back—including the evidence—so a partial
research write cannot leak into the project.

## 4. Inspect the populated projection

The same matrix now looks like this:

| Candidate | Technical strength | Interviewer confidence | Recommended role | Committee recommendation |
| --- | --- | ---: | --- | --- |
| Alex Rivera | Clear decomposition of queueing problems | 0.82 | Product Engineer | Unasked |
| Morgan Lee | Unasked | Unasked | Unasked | Unasked |

Three claims populated three cells in Alex's row. The committee column remains `Unasked` because
none of those commands asserted a committee decision. Each displayed value still links back to the
first interview evidence; the table is only the current projection, not the whole record.

Now record a second interviewer's independent assessment:

```bash
epiq --actor interviewer:liam record \
  --subject "Alex Rivera" \
  --source-type interview \
  --source-title "Alex Rivera systems interview" \
  --retrieved-at 2026-08-18 \
  --excerpt "Alex showed strong systems reasoning but communicated tradeoffs less clearly. Confidence: 0.68. Recommended for Research Engineer." \
  --valid-from 2026-08-18 \
  --answer technical_strength "Strong systems reasoning; tradeoffs less clearly communicated" \
  --answer interviewer_rating 0.68 \
  --answer recommended_role "Research Engineer"

epiq matrix --kind Candidate
epiq dossier "Alex Rivera"
```

Because those three fields have `cardinality: many`, the matrix retains both observations:

| Candidate | Technical strength | Interviewer confidence | Recommended role | Committee recommendation |
| --- | --- | ---: | --- | --- |
| Alex Rivera | Clear decomposition of queueing problems; Strong systems reasoning, tradeoffs less clearly communicated | 0.82; 0.68 | Product Engineer; Research Engineer | Unasked |
| Morgan Lee | Unasked | Unasked | Unasked | Unasked |

Epiq did not average `0.82` and `0.68`, select the newer role, or mark the cells contested. These
fields explicitly permit several supported observations. The matrix's JSON represents them in each
cell's `values` array.

The dossier then exposes the provenance hidden by that compact projection. Its lineage includes:

| Field | Value | Actor | Evidence |
| --- | --- | --- | --- |
| Technical strength | Clear decomposition of queueing problems | `interviewer:maya` | Alex Rivera technical interview |
| Technical strength | Strong systems reasoning; tradeoffs less clearly communicated | `interviewer:liam` | Alex Rivera systems interview |
| Interviewer confidence | 0.82 | `interviewer:maya` | Alex Rivera technical interview |
| Interviewer confidence | 0.68 | `interviewer:liam` | Alex Rivera systems interview |
| Recommended role | Product Engineer | `interviewer:maya` | Alex Rivera technical interview |
| Recommended role | Research Engineer | `interviewer:liam` | Alex Rivera systems interview |

The exact claim and evidence IDs also appear in the dossier JSON, allowing later review,
assessment, retraction, or supersession of one observation without disturbing the others.

## 5. Stage agent output for human review

An agent need not publish directly. It can propose a type-checked, evidence-backed claim:

```bash
epiq --actor agent:screening propose-claim \
  --subject "Alex Rivera" --question interviewer_rating --value 0.84 \
  --valid-from 2026-08-17 --evidence evd_REPLACE_WITH_RECORD_RESULT \
  --rationale 'Extracted from the committee packet'

epiq claim-proposals
```

Replace the placeholder with the `evidence_id` returned by `record`. The proposal is absent from the
current matrix until a reviewer runs `review-claims` with the returned proposal ID. Rejection also
remains in the audit history.

## 6. Understand disagreement versus contradiction

Multiple reviewer assessments are expected observations, hence `cardinality: many`. Two current
values for the single-valued `committee_recommendation` field indicate a contradiction that needs
explicit review:

```bash
epiq contradictions
```

Modeling cardinality correctly is more important than forcing every domain into one-cell semantics.

## Finished fixture

```bash
uv run examples/cli/hiring-committee/build.sh /tmp/epiq-hiring.sqlite
uv run epiq --db /tmp/epiq-hiring.sqlite dossier "Alex Rivera"
uv run epiq --db /tmp/epiq-hiring.sqlite query --kind Candidate \
  --where 'committee_recommendation=hire'
```

The fixture files, [schema.json](schema.json) and [writeback.json](writeback.json), are useful after
the tutorial as examples of idempotent setup and atomic multi-agent writeback.

<!-- epiq-example -->
```bash
examples/cli/hiring-committee/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Candidate \
  --where 'committee_recommendation=hire'
```
