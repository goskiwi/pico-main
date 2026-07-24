# pico-real-world-benchmark-v3-frozen

- Captured at: `2026-07-23T17:14:04.121866Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `2d9dfd2296b82d212a0397e74ca35ec25fc6e371`
- Working tree dirty: `True`
- Tasks: 5
- Repetitions: 1
- Fixture snapshot: `sha256:b6f06f654dd99b1abb217a0a54011b55241fe2aa7a98abd503d86ef759aec1ad`
- Evaluation snapshot: `sha256:8f98dfb3de88e42628f3241ed1e798d20f827aa1a9fdb6172801658dcff266ef`
- Run config: temperature=0.0, max_new_tokens=1024, verifier_timeout=90s
- Model cost scope: `attempt_parent_and_related_delegates`
- Duration semantics: model time is cumulative across model calls; agent duration is parent-attempt wall time and already includes delegate wait
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | responses_function | 80.0% | 4/5 | 4.20 | 5.20/0.00/5.20 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 56331/0/56331 | 18432/0/18432 | 3341/0/3341 | 21.61s/0.00s/21.61s | 22.94s |
| no_repo_map | responses_function | 80.0% | 4/5 | 6.00 | 7.00/0.00/7.00 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 85051/0/85051 | 42368/0/42368 | 5344/0/5344 | 32.75s/0.00s/32.75s | 34.09s |

## Ablation

- Pass-rate delta (full - no_repo_map): +0.0%
- Avg tool-step delta (full - no_repo_map): -1.80

## Failure breakdown

| Failure category | Count |
|---|---:|
| verifier_failed | 2 |

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| http_byte_range_forms | 1 | feature | full | PASS | PASS | 4 | 0 | 5/0/5 | 0/0/0 | 0/0/0 | 19.48s/0.00s/19.48s | 20.89s | - |
| stable_dependency_order | 1 | bugfix | full | FAIL | PASS | 4 | 0 | 5/0/5 | 0/0/0 | 0/0/0 | 13.83s/0.00s/13.83s | 15.10s | verifier_failed |
| atomic_batch_reservation | 1 | bugfix | full | PASS | PASS | 5 | 0 | 6/0/6 | 0/0/0 | 0/0/0 | 32.28s/0.00s/32.28s | 33.63s | - |
| single_pass_template_expansion | 1 | feature | full | PASS | PASS | 4 | 0 | 5/0/5 | 0/0/0 | 0/0/0 | 20.86s/0.00s/20.86s | 22.16s | - |
| atomic_name_index_rename | 1 | refactor | full | PASS | PASS | 4 | 0 | 5/0/5 | 0/0/0 | 0/0/0 | 21.62s/0.00s/21.62s | 22.94s | - |
| http_byte_range_forms | 1 | feature | no_repo_map | PASS | PASS | 5 | 0 | 6/0/6 | 0/0/0 | 0/0/0 | 22.43s/0.00s/22.43s | 23.75s | - |
| stable_dependency_order | 1 | bugfix | no_repo_map | FAIL | PASS | 6 | 0 | 7/0/7 | 0/0/0 | 0/0/0 | 30.47s/0.00s/30.47s | 31.80s | verifier_failed |
| atomic_batch_reservation | 1 | bugfix | no_repo_map | PASS | PASS | 5 | 0 | 6/0/6 | 0/0/0 | 0/0/0 | 20.81s/0.00s/20.81s | 22.12s | - |
| single_pass_template_expansion | 1 | feature | no_repo_map | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 40.61s/0.00s/40.61s | 41.99s | - |
| atomic_name_index_rename | 1 | refactor | no_repo_map | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 49.42s/0.00s/49.42s | 50.77s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- In schema v3, compatibility fields for model calls, tokens, failures, rejections, and protocols cover the parent plus related delegates; explicit P/D/T fields retain the breakdown.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
