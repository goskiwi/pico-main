# Runtime performance baseline

This is a diagnostic baseline, not a CI gate. It records the cost of the
pre-refactor JSONL writer and full replay path so later stages can be compared
against the same workload.

Run:

```bash
uv run python benchmarks/runtime_costs.py 1000 10000 100000
```

The fixture appends one initial task followed by small user-guidance events,
then measures full Run recovery and one bounded Context construction. Each size
uses a fresh temporary Run. `peak_rss` is the process-wide value reported by the
host and should only be compared when each size is run in a separate process.

Baseline captured on 2026-09-12 with Python 3.14 on macOS/APFS:

| Events | Log bytes | Append | Context | Full recovery |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 225,861 | 0.066 s | 0.917 s | 0.003 s |
| 10,000 | 2,297,861 | 0.698 s | 1.491 s | 0.032 s |
| 100,000 | 23,377,861 | 7.127 s | 14.643 s | 0.362 s |

The Stage 6 benchmark now runs each recovery mode in an isolated process and
compares full Replay with Checkpoint + Tail Replay, including peak memory.

## Stage 6 comparison

The post-refactor fixture compacts effective History every 1,000 events and
leaves a 100-event tail. Full Replay and Checkpoint + Tail run in separate child
processes so their peak RSS values are independent. Times are from the same
2026-09-12 macOS/APFS host; RSS is reported in bytes on this platform.

| Events | Append | Full recovery | Checkpoint recovery | Full peak RSS | Checkpoint peak RSS |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 0.073 s | 0.0032 s | 0.0023 s | 136.0 MB | 135.8 MB |
| 10,000 | 0.701 s | 0.0338 s | 0.0024 s | 143.4 MB | 136.5 MB |
| 100,000 | 7.197 s | 0.3753 s | 0.0024 s | 253.9 MB | 131.3 MB |

At 100,000 events, Checkpoint + Tail reduced measured recovery latency by about
153x and the isolated process peak RSS by about 48%. Incremental effective
History reduced the bounded Context construction from the 14.643 s baseline to
about 0.87 s after either recovery path. These observations document this host;
they are not acceptance thresholds.

## Current refactor measurement

Re-run on 2026-09-14 after the Runtime simplification, using the same command
and workload:

| Events | Log bytes | Append | Context | Full recovery | Checkpoint recovery | Full peak RSS | Checkpoint peak RSS |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 225,838 | 0.064 s | 0.109 s | 0.0036 s | 0.0024 s | 134.8 MB | 135.8 MB |
| 10,000 | 2,532,110 | 0.615 s | 0.112 s | 0.0333 s | 0.0027 s | 137.0 MB | 125.2 MB |
| 100,000 | 23,400,349 | 6.451 s | 0.0014 s | 0.3715 s | 0.0025 s | 181.1 MB | 49.3 MB |

At 100,000 events the checkpoint path replayed the bounded tail in about
1/147 of the full-replay time and used about 73% less peak memory. Context time
now measures projection into structured Model Messages rather than rendering and
tokenizing one flattened History string, so it is not directly comparable to the
older Context rows. Append time
remains linear and is not improved by Checkpoint; this benchmark therefore
does not claim to solve synchronous Event-log write cost.
