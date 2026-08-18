# Tutorial: model a forecasting tournament

A forecast update is not a correction: Alice's 0.30 on Monday and 0.55 on Tuesday are both valid
historical observations. Each submission is an `observation` row with a compound identity of event,
forecaster, and issue time, making repeated ingestion idempotent without relying on its display name.

```bash
examples/cli/forecasting-tournament/build.sh /tmp/epiq-forecasting.sqlite
epiq use /tmp/epiq-forecasting.sqlite
epiq --format table matrix --kind Forecast
```

| Forecast | Forecaster | Event | Probability | Issued at |
| --- | --- | --- | ---: | --- |
| Alice forecast 2026-08-17 | Alice | Rain in Boston on August 20 | 0.30 | 2026-08-17 09:00 ET |
| Alice forecast 2026-08-18 | Alice | Rain in Boston on August 20 | 0.55 | 2026-08-18 09:00 ET |
| Bob forecast 2026-08-17 | Bob | Rain in Boston on August 20 | 0.40 | 2026-08-17 10:00 ET |

```bash
epiq query --kind Forecast --where 'forecaster=Alice'
epiq --format table timeline --kind Forecast --question probability
```

The query reports `matched: 2`; the timeline orders 0.30, 0.40, then 0.55 by valid time.

```bash
epiq --format table aggregate \
  --kind Forecast --question probability --op avg --group-by forecaster
```

| group | avg | count |
| --- | ---: | ---: |
| Alice | 0.425 | 2 |
| Bob | 0.4 | 1 |

Unlike `aggregate`, `derive` writes a new claim and preserves its formula and input claims:

```bash
epiq --actor agent:ensemble derive \
  --subject "Rain in Boston on August 20" --question ensemble_probability \
  --operation avg --valid-from 2026-08-18 \
  --input-cell "Alice forecast 2026-08-17" probability \
  --input-cell "Alice forecast 2026-08-18" probability \
  --input-cell "Bob forecast 2026-08-17" probability

epiq dossier "Rain in Boston on August 20"
```

The resulting probability is approximately `0.4167`. Its dossier lineage records operation `avg`,
all three input claim IDs, and all three underlying evidence records.

```bash
epiq stale-derivations --kind ForecastEvent
```

This initially reports zero. Revising, retracting, or superseding one of the three input probability
claims makes the ensemble stale. A wholly new Forecast row is not yet detected as an input-set
change; selecting and rematerializing dynamic cohorts still requires external orchestration.

## Product gaps surfaced

- Forecast submissions must still be promoted to entities; Epiq has no first-class observation-series type.
- Grouped summaries exist, but there is no `latest by forecaster and event` or pivot operation.
- `derive` can persist an ensemble and detect changed dependencies, but it cannot detect that a new
  row should join a dynamic input cohort. Selecting the latest forecast per forecaster remains
  external orchestration.
- There are no Brier-score, resolution-event, or calibration-report primitives.
- Observation projection still requires a row per submission, even though identity is now explicit.

<!-- epiq-example -->
```bash
examples/cli/forecasting-tournament/build.sh "$EPIQ_EXAMPLE_DB"
epiq --quiet use "$EPIQ_EXAMPLE_DB"
epiq derive --subject "Rain in Boston on August 20" \
  --question ensemble_probability --operation avg --valid-from 2026-08-18 \
  --input-cell "Alice forecast 2026-08-17" probability \
  --input-cell "Alice forecast 2026-08-18" probability \
  --input-cell "Bob forecast 2026-08-17" probability
epiq --select count stale-derivations --kind ForecastEvent
epiq --select query.matched query --kind Forecast --where 'forecaster=Alice'
```
