# Structured Action Protocol Comparison

- Captured at: `2026-07-13T19:46:48.322546Z`
- Model: `gpt-5.4`
- Matched tasks: 10
- Fixture snapshot: `sha256:ba62646485ac83e54c39e86d6f5c53017c93406d5160ee7f5fc83834bad6657c`

| Metric | Text protocol | Structured actions | Delta |
|---|---:|---:|---:|
| Pass rate | 60.0% | 90.0% | +30.0% |
| Avg model calls | 9.50 | 6.40 | -3.10 |
| Action rejections | not recorded | 0 | n/a |

## Interpretation limit

Both legacy artifacts identify commit `3160f34`, but their older schema did not
record the working-tree dirty state or a complete runtime snapshot. Matching the
model, task IDs, and fixture snapshot makes this a useful historical before/after
observation; it does not establish that the action protocol was the only changed
variable, so the delta must not be presented as a strict causal ablation.

## Task details

| Task | Text | Structured | Calls before | Calls after |
|---|---:|---:|---:|---:|
| config_document_precedence | FAIL | FAIL | 4 | 4 |
| config_non_strict_mode | FAIL | PASS | 30 | 10 |
| config_preserve_equals | PASS | PASS | 10 | 7 |
| inventory_add_boundary_tests | PASS | PASS | 4 | 4 |
| inventory_normalize_sku | PASS | PASS | 6 | 4 |
| inventory_reserve_exact_stock | FAIL | PASS | 9 | 7 |
| inventory_total_value | PASS | PASS | 12 | 7 |
| priority_queue_stable_fifo | FAIL | PASS | 8 | 7 |
| retry_add_boundary_tests | PASS | PASS | 5 | 6 |
| retry_exact_attempt_budget | PASS | PASS | 7 | 8 |

The legacy comparison accepts artifacts when model, task IDs, and fixture snapshot
are identical. New causal comparisons should additionally run from the same clean
runtime commit, record the full evaluation snapshot, and use repeated attempts.
