# pico-real-world-benchmark-v1

- Captured at: `2026-07-13T19:41:24.812743Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Commit: `3160f344d4334baab0a51bf5760ede1c55063b60`
- Tasks: 10
- Repetitions: 1
- Fixture snapshot: `sha256:ba62646485ac83e54c39e86d6f5c53017c93406d5160ee7f5fc83834bad6657c`
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Protocol | Pass rate | Passed | Avg tools | Avg calls | Action rejects | Input tokens | Cached | Output | Avg duration |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | responses_function | 90.0% | 9/10 | 5.40 | 6.40 | 0.00 | 218380 | 190592 | 5665 | 26.86s |

## Failure breakdown

| Failure category | Count |
|---|---:|
| verifier_failed | 1 |

## Task details

| Task | Category | Variant | Result | Tools | Calls | Rejects | Duration | Failure |
|---|---|---|---:|---:|---:|---:|---:|---|
| inventory_normalize_sku | bugfix | full | PASS | 3 | 4 | 0 | 15.08s | - |
| inventory_reserve_exact_stock | bugfix | full | PASS | 6 | 7 | 0 | 30.93s | - |
| inventory_add_boundary_tests | test-addition | full | PASS | 3 | 4 | 0 | 18.86s | - |
| inventory_total_value | feature | full | PASS | 6 | 7 | 0 | 32.84s | - |
| config_preserve_equals | bugfix | full | PASS | 6 | 7 | 0 | 26.25s | - |
| config_non_strict_mode | feature | full | PASS | 9 | 10 | 0 | 39.22s | - |
| config_document_precedence | documentation | full | FAIL | 3 | 4 | 0 | 15.76s | verifier_failed |
| retry_exact_attempt_budget | bugfix | full | PASS | 7 | 8 | 0 | 36.84s | - |
| retry_add_boundary_tests | test-addition | full | PASS | 5 | 6 | 0 | 16.70s | - |
| priority_queue_stable_fifo | refactor | full | PASS | 6 | 7 | 0 | 36.08s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
