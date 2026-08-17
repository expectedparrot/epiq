# Hiring committee: multiple reviewers without flattening disagreement

This synthetic example treats candidates as rows and evaluation questions as fields. Interview
notes are non-web evidence identified by durable `urn:` locators. It intentionally records two
different technical assessments and two role recommendations for Alex Rivera.

> Hiring is a consequential domain. This fixture demonstrates provenance and disagreement, not an
> automated hiring policy. Do not collect protected traits, and keep employment decisions subject
> to appropriate human and legal review.

## Build it

From the repository root:

```bash
uv run examples/cli/hiring-committee/build.sh /tmp/epiq-hiring.sqlite
```

If `epiq` is installed globally, omit `uv run`. The builder creates `Role` and `Candidate` entities,
declares five typed fields, and submits [writeback.json](writeback.json) as one atomic transaction.

## Inspect the committee matrix

```bash
uv run epiq --db /tmp/epiq-hiring.sqlite matrix --kind Candidate
uv run epiq --db /tmp/epiq-hiring.sqlite dossier "Alex Rivera"
```

`technical_strength`, `interviewer_rating`, and `recommended_role` have `cardinality: many`, so Epiq
retains distinct reviewers' claims and evidence rather than inventing an average or winner.

Find candidates with an explicit committee hire recommendation:

```bash
uv run epiq --db /tmp/epiq-hiring.sqlite query --kind Candidate \
  --where '{"question":"committee_recommendation","op":"eq","value":"hire"}'
```

## Add review-first feedback

An agent can propose rather than publish another assessment:

```bash
uv run epiq --db /tmp/epiq-hiring.sqlite --actor agent:screening propose-claim \
  --subject "Morgan Lee" \
  --question interviewer_rating \
  --value 0.84 \
  --valid-from 2026-08-17 \
  --evidence evd_REPLACE_WITH_ID \
  --rationale "Extracted from the committee packet"

uv run epiq --db /tmp/epiq-hiring.sqlite claim-proposals
```

Use an evidence ID returned by `dossier` in place of the placeholder. A reviewer can then run
`review-claims ... --decision approved` or reject the proposal with a reason.

## What this exercises

- Evidence without a public URL
- `Probability`, `Enum[...]`, and `Ref[Role]`
- Multiple evidence-backed claims for one candidate and question
- Durable reviewer identity through `--actor`
- Queries, dossiers, and provisional claim review

## Executable documentation check

The documentation test runs this block verbatim with a fresh temporary database:

<!-- epiq-example -->
```bash
examples/cli/hiring-committee/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Candidate \
  --where 'committee_recommendation=hire'
```
