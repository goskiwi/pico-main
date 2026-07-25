# pico-repo-map-localization-v5-heldout

- Captured at: `2026-07-24T03:18:55.818661Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `363a8e832e5cc30620d43dd6a9566ad58ec9720c`
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
| full | dynamic | responses_function | 93.3% | 14/15 | 8.93 | 9.93/0.00/9.93 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 572949/0/572949 | 321280/0/321280 | 26881/0/26881 | 53.74s/0.00s/53.74s | 55.80s |
| repo_map_600 | 600 | responses_function | 93.3% | 14/15 | 9.00 | 10.00/0.00/10.00 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 551096/0/551096 | 289536/0/289536 | 28605/0/28605 | 57.78s/0.00s/57.78s | 59.63s |

## Repetition stability

| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |
|---|---:|---:|---:|---:|---:|
| full | 93.3% | 9.4% | 80.0% | 100.0% | 2/3 |
| repo_map_600 | 93.3% | 9.4% | 80.0% | 100.0% | 2/3 |

### Per repetition

| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |
|---|---:|---:|---:|---:|---:|
| full | 1 | 80.0% | 4/5 | 9.80 | 51.97s |
| full | 2 | 100.0% | 5/5 | 10.00 | 56.36s |
| full | 3 | 100.0% | 5/5 | 10.00 | 59.07s |
| repo_map_600 | 1 | 100.0% | 5/5 | 10.00 | 56.42s |
| repo_map_600 | 2 | 80.0% | 4/5 | 10.00 | 62.54s |
| repo_map_600 | 3 | 100.0% | 5/5 | 10.00 | 59.93s |

### Per-task stability

| Variant | Task | Pass rate | Passed | Outcome |
|---|---|---:|---:|---|
| full | cross_midnight_maintenance_window | 66.7% | 2/3 | mixed |
| full | discount_rule_precedence | 100.0% | 3/3 | always_passed |
| full | regional_inventory_allocation | 100.0% | 3/3 | always_passed |
| full | tenant_sticky_rollout_assignment | 100.0% | 3/3 | always_passed |
| full | transitive_incident_blockers | 100.0% | 3/3 | always_passed |
| repo_map_600 | cross_midnight_maintenance_window | 66.7% | 2/3 | mixed |
| repo_map_600 | discount_rule_precedence | 100.0% | 3/3 | always_passed |
| repo_map_600 | regional_inventory_allocation | 100.0% | 3/3 | always_passed |
| repo_map_600 | tenant_sticky_rollout_assignment | 100.0% | 3/3 | always_passed |
| repo_map_600 | transitive_incident_blockers | 100.0% | 3/3 | always_passed |

## Ablation

- Pass-rate delta (full - repo_map_600): +0.0%
- Avg tool-step delta (full - repo_map_600): -0.07

## Failure breakdown

| Failure category | Count |
|---|---:|
| verifier_failed | 2 |

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| regional_inventory_allocation | 1 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 46.93s/0.00s/46.93s | 49.25s | - |
| cross_midnight_maintenance_window | 1 | bugfix | full | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 44.26s/0.00s/44.26s | 46.55s | verifier_failed |
| discount_rule_precedence | 1 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 54.34s/0.00s/54.34s | 56.03s | - |
| tenant_sticky_rollout_assignment | 1 | bugfix | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 57.09s/0.00s/57.09s | 59.29s | - |
| transitive_incident_blockers | 1 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 46.54s/0.00s/46.54s | 48.75s | - |
| regional_inventory_allocation | 1 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 44.81s/0.00s/44.81s | 46.44s | - |
| cross_midnight_maintenance_window | 1 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 55.27s/0.00s/55.27s | 56.95s | - |
| discount_rule_precedence | 1 | feature | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 72.29s/0.00s/72.29s | 74.47s | - |
| tenant_sticky_rollout_assignment | 1 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 43.23s/0.00s/43.23s | 45.48s | - |
| transitive_incident_blockers | 1 | feature | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 56.52s/0.00s/56.52s | 58.75s | - |
| regional_inventory_allocation | 2 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 48.50s/0.00s/48.50s | 50.75s | - |
| cross_midnight_maintenance_window | 2 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 43.59s/0.00s/43.59s | 45.92s | - |
| discount_rule_precedence | 2 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 74.08s/0.00s/74.08s | 75.78s | - |
| tenant_sticky_rollout_assignment | 2 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 54.86s/0.00s/54.86s | 56.55s | - |
| transitive_incident_blockers | 2 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 50.64s/0.00s/50.64s | 52.77s | - |
| regional_inventory_allocation | 2 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 44.58s/0.00s/44.58s | 46.69s | - |
| cross_midnight_maintenance_window | 2 | bugfix | repo_map_600 | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 52.01s/0.00s/52.01s | 54.20s | verifier_failed |
| discount_rule_precedence | 2 | feature | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 93.82s/0.00s/93.82s | 94.81s | - |
| tenant_sticky_rollout_assignment | 2 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 59.65s/0.00s/59.65s | 61.88s | - |
| transitive_incident_blockers | 2 | feature | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 53.46s/0.00s/53.46s | 55.11s | - |
| regional_inventory_allocation | 3 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 46.76s/0.00s/46.76s | 48.40s | - |
| cross_midnight_maintenance_window | 3 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 46.40s/0.00s/46.40s | 48.60s | - |
| discount_rule_precedence | 3 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 54.65s/0.00s/54.65s | 56.35s | - |
| tenant_sticky_rollout_assignment | 3 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 40.97s/0.00s/40.97s | 43.23s | - |
| transitive_incident_blockers | 3 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 96.51s/0.00s/96.51s | 98.77s | - |
| regional_inventory_allocation | 3 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 66.03s/0.00s/66.03s | 68.25s | - |
| cross_midnight_maintenance_window | 3 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 52.88s/0.00s/52.88s | 55.14s | - |
| discount_rule_precedence | 3 | feature | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 68.26s/0.00s/68.26s | 69.86s | - |
| tenant_sticky_rollout_assignment | 3 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 55.20s/0.00s/55.20s | 56.80s | - |
| transitive_incident_blockers | 3 | feature | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 48.64s/0.00s/48.64s | 49.62s | - |

## Pre-registered decision gate

The gate was frozen in `v5-repo-map-budget-decision-protocol.md` at commit
`363a8e8`, before this first live-model run.

| Condition | Observed | Result |
|---|---|---|
| 600 passes at least 13/15 | 14/15 | PASS |
| 600 passes at least as many as full | 14 versus 14 | PASS |
| Reported input+output tokens per pass fall by at least 5% | 42,845 to 41,407; 3.36% lower | **FAIL** |
| No model failure, model_error, action rejection, or isolation failure | 0 for every category | PASS |
| Every 600 run has an effective map budget at most 600 | 15/15 recorded 600 | PASS |

Decision: keep the dynamic Repo Map default. Four of five conditions passed, but the
pre-registered cost threshold did not. The 600-token cap also used 3.8% fewer input
tokens but 6.4% more output tokens, 0.7% more tool steps, 0.7% more model calls, and
6.9% more average wall time.

Both variants failed `cross_midnight_maintenance_window` once, in different
repetitions, and passed the other four tasks 3/3. The two failures were incomplete
agent implementations rejected by the hidden verifier, not infrastructure failures.
V5 becomes a regression suite after this first result is inspected.

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- Model calls, tokens, failures, rejections, and protocols are reported explicitly for parent, delegates, and their total.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
