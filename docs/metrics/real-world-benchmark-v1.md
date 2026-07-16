# Pico Real-world Benchmark V1

- Captured at: `2026-07-13T18:39:17.470279Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Commit: `3160f344d4334baab0a51bf5760ede1c55063b60`
- Tasks: 10
- Repetitions: 1
- Fixture snapshot: `sha256:ba62646485ac83e54c39e86d6f5c53017c93406d5160ee7f5fc83834bad6657c`
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Pass rate | Passed | Avg tool steps | Avg model calls | Input tokens | Cached tokens | Output tokens | Avg duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| full | 60.0% | 6/10 | 5.30 | 9.50 | 323387 | 272128 | 5684 | 36.93s |

## Failure breakdown

| Failure category | Count |
|---|---:|
| retry_limit_reached | 1 |
| step_limit_reached | 1 |
| verifier_failed | 2 |

## Task details

| Task | Category | Variant | Result | Tools | Calls | Duration | Failure |
|---|---|---|---:|---:|---:|---:|---|
| inventory_normalize_sku | bugfix | full | PASS | 5 | 6 | 24.35s | - |
| inventory_reserve_exact_stock | bugfix | full | FAIL | 8 | 9 | 34.13s | step_limit_reached |
| inventory_add_boundary_tests | test-addition | full | PASS | 3 | 4 | 14.09s | - |
| inventory_total_value | feature | full | PASS | 9 | 12 | 58.95s | - |
| config_preserve_equals | bugfix | full | PASS | 7 | 10 | 45.72s | - |
| config_non_strict_mode | feature | full | FAIL | 1 | 30 | 91.71s | retry_limit_reached |
| config_document_precedence | documentation | full | FAIL | 3 | 4 | 20.54s | verifier_failed |
| retry_exact_attempt_budget | bugfix | full | PASS | 6 | 7 | 24.11s | - |
| retry_add_boundary_tests | test-addition | full | PASS | 4 | 5 | 22.86s | - |
| priority_queue_stable_fifo | refactor | full | FAIL | 7 | 8 | 32.79s | verifier_failed |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
