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

Stage 2 reruns this command after the durable tool-event rewrite. Stage 6 runs
each size in an isolated process and compares full replay with Checkpoint + Tail
Replay, including peak memory.
