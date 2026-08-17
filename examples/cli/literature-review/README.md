# Stress test: a research literature review

Papers are sources, but their individual findings also need rows so results can be compared without
claiming the papers themselves are contradictory.

```bash
uv run examples/cli/literature-review/build.sh /tmp/epiq-literature.sqlite
uv run epiq --db /tmp/epiq-literature.sqlite --format table matrix --kind Finding
```

| Finding | Paper | Research question | Effect | Interpretation |
| --- | --- | --- | ---: | --- |
| Study A productivity finding | Remote Work Study A | Effect of remote work on productivity | 0.18 | positive |
| Study B productivity finding | Remote Work Study B | Effect of remote work on productivity | -0.05 | null |

```bash
uv run epiq --db /tmp/epiq-literature.sqlite query --kind Finding --where 'effect_size > 0'
```

Output: `matched: 1`, returning Study A's finding and its structured `{"page":12}` locator.

## Product gaps surfaced

- Sources can now link to their Paper entity and carry structured page/table/section locators.
- Findings are claim-like entities; there is no native claim-about-claim or replication relation.
- Meta-analysis needs grouping, weighting, uncertainty intervals, and derived estimates.
- Evidence-quality rubrics exist only as free-form assessments, not reusable schemas.

<!-- epiq-example -->
```bash
examples/cli/literature-review/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Finding --where 'effect_size > 0'
```
