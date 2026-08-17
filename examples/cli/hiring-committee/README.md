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

## 3. Add private interview notes as evidence

There is no public URL, and Epiq does not require one:

```bash
MAYA_NOTES=$(epiq --actor interviewer:maya evidence \
  --type interview --title 'Alex Rivera technical interview' \
  --retrieved-at 2026-08-17 \
  --excerpt 'Alex decomposed the queueing problem clearly. Confidence: 0.82. Recommended for Product Engineer.' \
  | jq -r .evidence_id)
```

The source type says how this information was obtained. The actor says who introduced it into the
ledger. A production system can point the source locator at a private document without pretending
it is a public webpage.

## 4. Make several claims from one note

```bash
epiq --actor interviewer:maya assert \
  --subject "Alex Rivera" --question technical_strength \
  --value 'Clear decomposition of queueing problems' \
  --valid-from 2026-08-17 --evidence "$MAYA_NOTES" --confidence medium

epiq --actor interviewer:maya assert \
  --subject "Alex Rivera" --question interviewer_rating --value 0.82 \
  --valid-from 2026-08-17 --evidence "$MAYA_NOTES" --confidence medium

epiq --actor interviewer:maya assert \
  --subject "Alex Rivera" --question recommended_role --value "Product Engineer" \
  --valid-from 2026-08-17 --evidence "$MAYA_NOTES" --confidence medium
```

Now add another interviewer's note and claims using `--actor interviewer:...`. Because these fields
have `cardinality: many`, Epiq retains both reviewers' evidence and values rather than averaging
them or choosing a winner.

```bash
epiq dossier "Alex Rivera"
epiq matrix --kind Candidate
```

## 5. Stage agent output for human review

An agent need not publish directly. It can propose a type-checked, evidence-backed claim:

```bash
epiq --actor agent:screening propose-claim \
  --subject "Alex Rivera" --question interviewer_rating --value 0.84 \
  --valid-from 2026-08-17 --evidence "$MAYA_NOTES" \
  --rationale 'Extracted from the committee packet'

epiq claim-proposals
```

The proposal is absent from the current matrix until a reviewer runs `review-claims` with the
returned proposal ID. Rejection also remains in the audit history.

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
