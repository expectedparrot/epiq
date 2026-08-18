# Tutorial: calculate startup unit economics with row formulas

This example starts with sourced operating metrics and declares three calculations once in the
schema. `materialize` then applies those formulas to every startup whose inputs are available.

Build the base facts and inspect them before calculating anything:

```bash
uv run examples/cli/startup-unit-economics/build.sh /tmp/epiq-startup-unit-economics.sqlite
uv run epiq --db /tmp/epiq-startup-unit-economics.sqlite --format table matrix --kind Startup
```

The three formula fields are equivalent to spreadsheet formulas, but store stable field names:

| Derived field | Spreadsheet notation | Stored expression |
| --- | --- | --- |
| MRR per customer | `=B1/C1` | `x0/x1` |
| ARR per employee | `=(B1*12)/D1` | `(x0*12)/x1` |
| Age in 2026 | `=2026-E1` | `2026-x0` |

Materialize every ready formula:

```bash
uv run epiq --db /tmp/epiq-startup-unit-economics.sqlite materialize \
  --kind Startup --valid-from 2026-08-18

uv run epiq --db /tmp/epiq-startup-unit-economics.sqlite --format table matrix \
  --kind Startup --questions monthly_recurring_revenue,customers,employees,founded_year,revenue_per_customer,annualized_revenue_per_employee,company_age
```

| Startup | MRR | Customers | Employees | Founded | MRR/customer | ARR/employee | Age |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Acorn Analytics | 120,000 | 300 | 24 | 2020 | 400 | 60,000 | 6 |
| Beacon AI | 90,000 | 120 | 15 | 2022 | 750 | 72,000 | 4 |
| Cobalt Systems | 45,000 | — | 10 | — | — | 54,000 | — |

Cobalt demonstrates partial materialization: ARR per employee is ready, while the other two
formulas are reported as `skipped` because their inputs are missing. Epiq does not turn missing
research into zero.

Each calculated claim inherits its input evidence and records the input claim IDs as `operand`
dependencies. If Acorn later reports revised MRR, the historical calculation remains intact but is
flagged as stale:

```bash
uv run epiq --db /tmp/epiq-startup-unit-economics.sqlite stale-derivations --kind Startup
```

Re-run `materialize` after reviewing the changed dependency to create current derived claims.
Division by zero and unsafe expressions fail without writing a partial result.

<!-- epiq-example -->
```bash
examples/cli/startup-unit-economics/build.sh "$EPIQ_EXAMPLE_DB"
epiq --db "$EPIQ_EXAMPLE_DB" materialize --kind Startup --valid-from 2026-08-18
epiq --db "$EPIQ_EXAMPLE_DB" --select count stale-derivations --kind Startup
epiq --db "$EPIQ_EXAMPLE_DB" --select rows matrix --kind Startup
```
