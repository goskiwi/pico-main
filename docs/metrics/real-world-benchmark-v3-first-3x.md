# pico-real-world-benchmark-v3-frozen

- Captured at: `2026-07-17T00:33:59.035614Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `0897195d7adc82713e88b07a5c1284c6494e454c`
- Working tree dirty: `False`
- Tasks: 5
- Repetitions: 3
- Fixture snapshot: `sha256:b6f06f654dd99b1abb217a0a54011b55241fe2aa7a98abd503d86ef759aec1ad`
- Evaluation snapshot: `sha256:8f98dfb3de88e42628f3241ed1e798d20f827aa1a9fdb6172801658dcff266ef`
- Run config: temperature=0.0, max_new_tokens=1024, verifier_timeout=90s
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Protocol | Pass rate | Passed | Avg tools | Avg calls | Action rejects | Input tokens | Cached | Output | Avg duration |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | responses_function | 86.7% | 13/15 | 5.73 | 6.73 | 0.00 | 254593 | 198656 | 12852 | 33.52s |

## Repetition stability

| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |
|---|---:|---:|---:|---:|---:|
| full | 86.7% | 9.4% | 80.0% | 100.0% | 1/3 |

### Per repetition

| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |
|---|---:|---:|---:|---:|---:|
| full | 1 | 100.0% | 5/5 | 7.20 | 39.72s |
| full | 2 | 80.0% | 4/5 | 6.60 | 30.57s |
| full | 3 | 80.0% | 4/5 | 6.40 | 30.26s |

### Per-task stability

| Variant | Task | Pass rate | Passed | Outcome |
|---|---|---:|---:|---|
| full | atomic_batch_reservation | 100.0% | 3/3 | always_passed |
| full | atomic_name_index_rename | 100.0% | 3/3 | always_passed |
| full | http_byte_range_forms | 100.0% | 3/3 | always_passed |
| full | single_pass_template_expansion | 100.0% | 3/3 | always_passed |
| full | stable_dependency_order | 33.3% | 1/3 | mixed |

## Failure breakdown

| Failure category | Count |
|---|---:|
| verifier_failed | 2 |

## Task details

| Task | Rep | Category | Variant | Result | Tools | Calls | Rejects | Duration | Failure |
|---|---:|---|---|---:|---:|---:|---:|---:|---|
| http_byte_range_forms | 1 | feature | full | PASS | 5 | 6 | 0 | 28.69s | - |
| stable_dependency_order | 1 | bugfix | full | PASS | 7 | 8 | 0 | 50.64s | - |
| atomic_batch_reservation | 1 | bugfix | full | PASS | 7 | 8 | 0 | 48.24s | - |
| single_pass_template_expansion | 1 | feature | full | PASS | 6 | 7 | 0 | 33.74s | - |
| atomic_name_index_rename | 1 | refactor | full | PASS | 6 | 7 | 0 | 37.27s | - |
| http_byte_range_forms | 2 | feature | full | PASS | 5 | 6 | 0 | 23.61s | - |
| stable_dependency_order | 2 | bugfix | full | FAIL | 6 | 7 | 0 | 31.37s | verifier_failed |
| atomic_batch_reservation | 2 | bugfix | full | PASS | 6 | 7 | 0 | 35.68s | - |
| single_pass_template_expansion | 2 | feature | full | PASS | 5 | 6 | 0 | 26.89s | - |
| atomic_name_index_rename | 2 | refactor | full | PASS | 6 | 7 | 0 | 35.30s | - |
| http_byte_range_forms | 3 | feature | full | PASS | 5 | 6 | 0 | 26.37s | - |
| stable_dependency_order | 3 | bugfix | full | FAIL | 6 | 7 | 0 | 38.04s | verifier_failed |
| atomic_batch_reservation | 3 | bugfix | full | PASS | 6 | 7 | 0 | 35.79s | - |
| single_pass_template_expansion | 3 | feature | full | PASS | 5 | 6 | 0 | 24.79s | - |
| atomic_name_index_rename | 3 | refactor | full | PASS | 5 | 6 | 0 | 26.30s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
