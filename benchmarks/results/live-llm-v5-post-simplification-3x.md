# pico-repo-map-localization-v5-heldout

- Captured at: `2026-07-25T12:24:12.036530Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `0817dbb7b99ccdace87b0682d3f48fa0cf9a4555`
- Working tree dirty: `False`
- Tasks: 5
- Repetitions: 3
- Fixture snapshot: `sha256:4425899ac4accc7f7526891ab3440e9566282c98c36a6af5f76b841e004da99a`
- Evaluation snapshot: `sha256:84943a3e4d9e220cc899042ce9a808ced8d554484c66969998e4477852075ef4`
- Run config: temperature=0.0, max_new_tokens=1024, verifier_timeout=90s
- Model cost scope: `attempt_parent_and_related_delegates`
- Duration semantics: model time is cumulative across model calls; agent duration is parent-attempt wall time and already includes delegate wait
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Map cap | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | dynamic | responses_function | 100.0% | 15/15 | 9.80 | 10.80/0.00/10.80 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 689129/0/689129 | 393600/0/393600 | 29722/0/29722 | 58.92s/0.00s/58.92s | 61.26s |

## Repetition stability

| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |
|---|---:|---:|---:|---:|---:|
| full | 100.0% | 0.0% | 100.0% | 100.0% | 3/3 |

### Per repetition

| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |
|---|---:|---:|---:|---:|---:|
| full | 1 | 100.0% | 5/5 | 11.00 | 61.50s |
| full | 2 | 100.0% | 5/5 | 10.80 | 57.38s |
| full | 3 | 100.0% | 5/5 | 10.60 | 64.89s |

### Per-task stability

| Variant | Task | Pass rate | Passed | Outcome |
|---|---|---:|---:|---|
| full | cross_midnight_maintenance_window | 100.0% | 3/3 | always_passed |
| full | discount_rule_precedence | 100.0% | 3/3 | always_passed |
| full | regional_inventory_allocation | 100.0% | 3/3 | always_passed |
| full | tenant_sticky_rollout_assignment | 100.0% | 3/3 | always_passed |
| full | transitive_incident_blockers | 100.0% | 3/3 | always_passed |

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| regional_inventory_allocation | 1 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 55.24s/0.00s/55.24s | 57.37s | - |
| cross_midnight_maintenance_window | 1 | bugfix | full | PASS | PASS | 13 | 0 | 14/0/14 | 0/0/0 | 0/0/0 | 74.77s/0.00s/74.77s | 78.20s | - |
| discount_rule_precedence | 1 | feature | full | PASS | PASS | 10 | 0 | 11/0/11 | 0/0/0 | 0/0/0 | 62.29s/0.00s/62.29s | 64.41s | - |
| tenant_sticky_rollout_assignment | 1 | bugfix | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 40.74s/0.00s/40.74s | 42.78s | - |
| transitive_incident_blockers | 1 | feature | full | PASS | PASS | 10 | 0 | 11/0/11 | 0/0/0 | 0/0/0 | 62.63s/0.00s/62.63s | 64.76s | - |
| regional_inventory_allocation | 2 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 48.90s/0.00s/48.90s | 50.97s | - |
| cross_midnight_maintenance_window | 2 | bugfix | full | PASS | PASS | 11 | 0 | 12/0/12 | 0/0/0 | 0/0/0 | 53.49s/0.00s/53.49s | 56.23s | - |
| discount_rule_precedence | 2 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 57.28s/0.00s/57.28s | 59.38s | - |
| tenant_sticky_rollout_assignment | 2 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 54.77s/0.00s/54.77s | 56.92s | - |
| transitive_incident_blockers | 2 | feature | full | PASS | PASS | 11 | 0 | 12/0/12 | 0/0/0 | 0/0/0 | 61.10s/0.00s/61.10s | 63.39s | - |
| regional_inventory_allocation | 3 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 49.53s/0.00s/49.53s | 51.83s | - |
| cross_midnight_maintenance_window | 3 | bugfix | full | PASS | PASS | 11 | 0 | 12/0/12 | 0/0/0 | 0/0/0 | 90.58s/0.00s/90.58s | 93.26s | - |
| discount_rule_precedence | 3 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 62.70s/0.00s/62.70s | 64.80s | - |
| tenant_sticky_rollout_assignment | 3 | bugfix | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 42.64s/0.00s/42.64s | 44.69s | - |
| transitive_incident_blockers | 3 | feature | full | PASS | PASS | 11 | 0 | 12/0/12 | 0/0/0 | 0/0/0 | 67.18s/0.00s/67.18s | 69.86s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- Model calls, tokens, failures, rejections, protocols, and duration are reported explicitly for parent, delegates, and their total.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
