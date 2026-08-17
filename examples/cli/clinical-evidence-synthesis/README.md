# Stress test: clinical evidence synthesis

This synthetic example is about data mechanics, not medical guidance. Each paper finding is an
observation with a compound identity; its evidence links to the Study entity and an exact table.

```bash
uv run examples/cli/clinical-evidence-synthesis/build.sh /tmp/epiq-clinical.sqlite
uv run epiq --db /tmp/epiq-clinical.sqlite --format table matrix --kind Finding
```

| Finding | Study | Effect size | Sample size |
| --- | --- | ---: | ---: |
| Alpha primary finding | Study Alpha | 0.2 | 100 |
| Beta primary finding | Study Beta | 0.5 | 200 |

Persist a sample-size-weighted estimate:

```bash
uv run epiq --db /tmp/epiq-clinical.sqlite --actor agent:review derive \
  --subject "Sleep intervention review" --question pooled_effect \
  --operation weighted_avg --parameters '{"weights":[100,200]}' \
  --valid-from 2026-08-17 \
  --input-cell "Alpha primary finding" effect \
  --input-cell "Beta primary finding" effect
```

Output value: `0.4`. The dossier contains both input claim IDs, both evidence records, their Study
entity links, and locators `page 14/table 2` and `page 9/table 3`.

Remaining gap: real meta-analysis needs uncertainty types and formulas whose weights can themselves
reference claims instead of being copied into parameters.

<!-- epiq-example -->
```bash
examples/cli/clinical-evidence-synthesis/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" derive --subject "Sleep intervention review" \
  --question pooled_effect --operation weighted_avg --parameters '{"weights":[100,200]}' \
  --valid-from 2026-08-17 --input-cell "Alpha primary finding" effect \
  --input-cell "Beta primary finding" effect
epiq --db "$EPIQ_EXAMPLE_DB" --select rows matrix --kind Review
```
