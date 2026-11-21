# Ledger performance baseline

Epiq includes reproducible synthetic profiles for detecting storage regressions. Profiles contain
one canonical transaction per file, a causal chain, one entity-kind definition, and entity records
with representative attributes.

| Profile | Transactions |
|---|---:|
| `small` | 100 |
| `medium` | 1,000 |
| `large` | 10,000 |

Run a profile without creating persistent project data:

```bash
epiq benchmark --synthetic medium --iterations 3
```

## Initial baseline

Measured 2026-11-21 on Apple arm64, macOS 15.5, Python 3.12.7. Times are medians in
milliseconds; the large profile has one sample and should be treated as directional.

| Metric | Small | Medium | Large |
|---|---:|---:|---:|
| Generate ledger | 16.7 | 141.1 | 1,491.3 |
| Validate ledger | 16.9 | 193.3 | 4,700.3 |
| Cold rebuild | 43.9 | 252.1 | 5,158.9 |
| No-op incremental open | 22.9 | 203.3 | 4,777.0 |
| Projection read | 0.8 | 0.9 | 1.2 |
| Semantic fingerprint | 4.1 | 23.1 | 218.1 |

The projection queries scale well. The dominant large-ledger cost is repeatedly discovering,
reading, parsing, hashing, and validating transaction files—even for a no-op open. This evidence
does not yet justify snapshots or transaction packs: first cache transaction discovery and parsed
content within an open repository operation, then repeat the baseline. Packs should be considered
only if cold-start file traversal remains the bottleneck after that optimization.

## Parse-reuse result

Commit-time optimization now reuses the already parsed, causally ordered transaction set when
computing the frontier, transaction-set digest, and projection metadata. Each command still reads
fresh state from disk, so a separate process or Git update is never hidden by a persistent cache.

The large-profile single-sample comparison on the same machine was:

| Metric | Before | After | Reduction |
|---|---:|---:|---:|
| Validate ledger | 4,700.3 | 2,075.8 | 56% |
| Cold rebuild | 5,158.9 | 2,511.2 | 51% |
| No-op incremental open | 4,777.0 | 2,193.1 | 54% |

File discovery and parsing remain the principal no-op cost at 10,000 files, but the result is not
yet severe enough to add a second authoritative representation. The next scale optimization, if
needed, should use manifest metadata to avoid opening unchanged files while retaining canonical
transaction files as the only durable source of truth.

## CI regression guard

`benchmarks/medium-ci-thresholds.json` contains intentionally broad ceilings for heterogeneous CI
machines. They detect order-of-magnitude regressions rather than enforcing this laptop's timings:

```bash
epiq benchmark --synthetic medium --iterations 1 \
  --thresholds benchmarks/medium-ci-thresholds.json --fail-on-regression
```

Thresholds are explicit and environment-specific. Correctness tests remain independent of timing.
