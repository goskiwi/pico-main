# pico-real-world-benchmark-v2-heldout

- Captured at: `2026-07-13T19:43:55.422756Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Commit: `3160f344d4334baab0a51bf5760ede1c55063b60`
- Tasks: 5
- Repetitions: 1
- Fixture snapshot: `sha256:b6c80d23edd3fba9b200f0f2f31349f850adb161d72de0006bfb9c4da0b1aea1`
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Protocol | Pass rate | Passed | Avg tools | Avg calls | Action rejects | Input tokens | Cached | Output | Avg duration |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | responses_function | 100.0% | 5/5 | 5.20 | 6.20 | 0.00 | 106931 | 91520 | 3310 | 27.21s |

## Task details

| Task | Category | Variant | Result | Tools | Calls | Rejects | Duration | Failure |
|---|---|---|---:|---:|---:|---:|---:|---|
| url_query_before_fragment | bugfix | full | PASS | 3 | 4 | 0 | 15.03s | - |
| ttl_cache_expiration | feature | full | PASS | 6 | 7 | 0 | 38.35s | - |
| csv_quoted_record | bugfix | full | PASS | 6 | 7 | 0 | 26.68s | - |
| event_bus_unsubscribe_tests | test-addition | full | PASS | 5 | 6 | 0 | 21.43s | - |
| lru_update_recency | refactor | full | PASS | 6 | 7 | 0 | 34.55s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
