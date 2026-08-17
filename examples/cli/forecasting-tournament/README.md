# Stress test: a forecasting tournament

A forecast update is not a correction: Alice's 0.30 on Monday and 0.55 on Tuesday are both valid
historical observations. To avoid treating them as contradictory values in one cell, model each
submission as a `Forecast` row.

```bash
uv run examples/cli/forecasting-tournament/build.sh /tmp/epiq-forecasting.sqlite
uv run epiq --db /tmp/epiq-forecasting.sqlite matrix --kind Forecast
```

| Forecast | Forecaster | Event | Probability | Issued at |
| --- | --- | --- | ---: | --- |
| Alice forecast 2026-08-17 | Alice | Rain in Boston on August 20 | 0.30 | 2026-08-17 09:00 ET |
| Alice forecast 2026-08-18 | Alice | Rain in Boston on August 20 | 0.55 | 2026-08-18 09:00 ET |
| Bob forecast 2026-08-17 | Bob | Rain in Boston on August 20 | 0.40 | 2026-08-17 10:00 ET |

```bash
uv run epiq --db /tmp/epiq-forecasting.sqlite query --kind Forecast --where 'forecaster=Alice'
uv run epiq --db /tmp/epiq-forecasting.sqlite timeline --kind Forecast --question probability
```

The query reports `matched: 2`; the timeline orders 0.30, 0.40, then 0.55 by valid time.

## Product gaps surfaced

- Forecast submissions must be promoted to entities; Epiq has no first-class observation-series type.
- There is no `latest by forecaster and event`, grouping, or pivot operation.
- There are no derived ensemble forecasts, Brier scores, resolution events, or calibration reports.
- A forecast's identity is encoded in its name rather than a compound uniqueness constraint.

<!-- epiq-example -->
```bash
examples/cli/forecasting-tournament/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" --select query.matched query --kind Forecast --where 'forecaster=Alice'
```
