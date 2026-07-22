# pico-real-world-benchmark-v3-frozen

> **Status: current negative evidence.** This retained regression explains why the
> tested prompt change was rolled back; it is not a new held-out result. See the
> [metrics evidence map](README.md).

- Captured at: `2026-07-17T03:26:46.461184Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `82106a129243abd20e9bed0a358afc706055519e`
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
| full | responses_function | 80.0% | 12/15 | 6.33 | 7.33 | 0.00 | 292265 | 235008 | 16050 | 46.08s |

## Repetition stability

| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |
|---|---:|---:|---:|---:|---:|
| full | 80.0% | 0.0% | 80.0% | 80.0% | 0/3 |

### Per repetition

| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |
|---|---:|---:|---:|---:|---:|
| full | 1 | 80.0% | 4/5 | 7.40 | 47.64s |
| full | 2 | 80.0% | 4/5 | 7.20 | 43.59s |
| full | 3 | 80.0% | 4/5 | 7.40 | 47.01s |

### Per-task stability

| Variant | Task | Pass rate | Passed | Outcome |
|---|---|---:|---:|---|
| full | atomic_batch_reservation | 100.0% | 3/3 | always_passed |
| full | atomic_name_index_rename | 100.0% | 3/3 | always_passed |
| full | http_byte_range_forms | 100.0% | 3/3 | always_passed |
| full | single_pass_template_expansion | 100.0% | 3/3 | always_passed |
| full | stable_dependency_order | 0.0% | 0/3 | always_failed |

## Failure breakdown

| Failure category | Count |
|---|---:|
| verifier_failed | 3 |

## Task details

| Task | Rep | Category | Variant | Result | Tools | Calls | Rejects | Duration | Failure |
|---|---:|---|---|---:|---:|---:|---:|---:|---|
| http_byte_range_forms | 1 | feature | full | PASS | 6 | 7 | 0 | 42.75s | - |
| stable_dependency_order | 1 | bugfix | full | FAIL | 6 | 7 | 0 | 43.93s | verifier_failed |
| atomic_batch_reservation | 1 | bugfix | full | PASS | 6 | 7 | 0 | 42.24s | - |
| single_pass_template_expansion | 1 | feature | full | PASS | 6 | 7 | 0 | 51.90s | - |
| atomic_name_index_rename | 1 | refactor | full | PASS | 8 | 9 | 0 | 57.39s | - |
| http_byte_range_forms | 2 | feature | full | PASS | 6 | 7 | 0 | 50.35s | - |
| stable_dependency_order | 2 | bugfix | full | FAIL | 7 | 8 | 0 | 40.64s | verifier_failed |
| atomic_batch_reservation | 2 | bugfix | full | PASS | 6 | 7 | 0 | 35.54s | - |
| single_pass_template_expansion | 2 | feature | full | PASS | 6 | 7 | 0 | 48.96s | - |
| atomic_name_index_rename | 2 | refactor | full | PASS | 6 | 7 | 0 | 42.43s | - |
| http_byte_range_forms | 3 | feature | full | PASS | 6 | 7 | 0 | 40.58s | - |
| stable_dependency_order | 3 | bugfix | full | FAIL | 7 | 8 | 0 | 50.58s | verifier_failed |
| atomic_batch_reservation | 3 | bugfix | full | PASS | 6 | 7 | 0 | 57.30s | - |
| single_pass_template_expansion | 3 | feature | full | PASS | 7 | 8 | 0 | 39.99s | - |
| atomic_name_index_rename | 3 | refactor | full | PASS | 6 | 7 | 0 | 46.58s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
