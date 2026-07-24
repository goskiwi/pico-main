# pico-repo-map-localization-v4-frozen

- Captured at: `2026-07-23T17:56:16.803918Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `016c618cb00d182b6ef7a056c8ddc01a9edd7635`
- Working tree dirty: `False`
- Tasks: 5
- Repetitions: 3
- Fixture snapshot: `sha256:3417db101b69581cef16e87129dc4d1d648b97cc283e28887afddfae3c5137dd`
- Evaluation snapshot: `sha256:7f0029bc8bf8779432f1dc72fd903fac3ab07521c68aad08924c08e56d6e7824`
- Run config: temperature=0.0, max_new_tokens=1024, verifier_timeout=90s
- Model cost scope: `attempt_parent_and_related_delegates`
- Duration semantics: model time is cumulative across model calls; agent duration is parent-attempt wall time and already includes delegate wait
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | responses_function | 86.7% | 13/15 | 8.07 | 9.07/0.00/9.07 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 446783/0/446783 | 212352/0/212352 | 17462/0/17462 | 39.39s/0.00s/39.39s | 40.92s |
| no_repo_map | responses_function, runtime_guard | 40.0% | 6/15 | 8.60 | 9.60/0.00/9.60 | 0.00 | 0.00/0.00/0.00 | 0.47/0.00/0.47 | 336633/0/336633 | 95872/0/95872 | 10384/0/10384 | 32.60s/0.00s/32.60s | 33.50s |

## Repetition stability

| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |
|---|---:|---:|---:|---:|---:|
| full | 86.7% | 9.4% | 80.0% | 100.0% | 1/3 |
| no_repo_map | 40.0% | 16.3% | 20.0% | 60.0% | 0/3 |

### Per repetition

| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |
|---|---:|---:|---:|---:|---:|
| full | 1 | 80.0% | 4/5 | 9.20 | 38.33s |
| full | 2 | 100.0% | 5/5 | 8.80 | 43.81s |
| full | 3 | 80.0% | 4/5 | 9.20 | 40.62s |
| no_repo_map | 1 | 20.0% | 1/5 | 9.60 | 27.15s |
| no_repo_map | 2 | 40.0% | 2/5 | 9.60 | 34.08s |
| no_repo_map | 3 | 60.0% | 3/5 | 9.60 | 39.28s |

### Per-task stability

| Variant | Task | Pass rate | Passed | Outcome |
|---|---|---:|---:|---|
| full | catalog_rename_cache_invalidation | 33.3% | 1/3 | mixed |
| full | inherited_role_permissions | 100.0% | 3/3 | always_passed |
| full | notification_locale_fallback | 100.0% | 3/3 | always_passed |
| full | regional_checkout_shipping | 100.0% | 3/3 | always_passed |
| full | tenant_scoped_webhook_dedup | 100.0% | 3/3 | always_passed |
| no_repo_map | catalog_rename_cache_invalidation | 0.0% | 0/3 | always_failed |
| no_repo_map | inherited_role_permissions | 33.3% | 1/3 | mixed |
| no_repo_map | notification_locale_fallback | 100.0% | 3/3 | always_passed |
| no_repo_map | regional_checkout_shipping | 66.7% | 2/3 | mixed |
| no_repo_map | tenant_scoped_webhook_dedup | 0.0% | 0/3 | always_failed |

## Ablation

- Pass-rate delta (full - no_repo_map): +46.7%
- Avg tool-step delta (full - no_repo_map): -0.53

## Failure breakdown

| Failure category | Count |
|---|---:|
| step_limit_reached | 7 |
| verifier_failed | 4 |

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| regional_checkout_shipping | 1 | bugfix | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 28.93s/0.00s/28.93s | 30.48s | - |
| tenant_scoped_webhook_dedup | 1 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 37.46s/0.00s/37.46s | 39.06s | - |
| inherited_role_permissions | 1 | feature | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 38.91s/0.00s/38.91s | 40.41s | - |
| catalog_rename_cache_invalidation | 1 | refactor | full | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 47.05s/0.00s/47.05s | 48.12s | verifier_failed |
| notification_locale_fallback | 1 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 31.98s/0.00s/31.98s | 33.59s | - |
| regional_checkout_shipping | 1 | bugfix | no_repo_map | FAIL | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 1/0/1 | 21.31s/0.00s/21.31s | 22.12s | step_limit_reached |
| tenant_scoped_webhook_dedup | 1 | bugfix | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 1/0/1 | 25.84s/0.00s/25.84s | 26.86s | step_limit_reached |
| inherited_role_permissions | 1 | feature | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 1/0/1 | 22.78s/0.00s/22.78s | 23.70s | step_limit_reached |
| catalog_rename_cache_invalidation | 1 | refactor | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 1/0/1 | 27.09s/0.00s/27.09s | 28.04s | step_limit_reached |
| notification_locale_fallback | 1 | bugfix | no_repo_map | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 33.99s/0.00s/33.99s | 35.03s | - |
| regional_checkout_shipping | 2 | bugfix | full | PASS | PASS | 6 | 0 | 7/0/7 | 0/0/0 | 0/0/0 | 24.81s/0.00s/24.81s | 26.29s | - |
| tenant_scoped_webhook_dedup | 2 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 47.40s/0.00s/47.40s | 49.08s | - |
| inherited_role_permissions | 2 | feature | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 49.51s/0.00s/49.51s | 51.07s | - |
| catalog_rename_cache_invalidation | 2 | refactor | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 64.88s/0.00s/64.88s | 66.57s | - |
| notification_locale_fallback | 2 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 23.89s/0.00s/23.89s | 26.01s | - |
| regional_checkout_shipping | 2 | bugfix | no_repo_map | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 28.44s/0.00s/28.44s | 29.34s | - |
| tenant_scoped_webhook_dedup | 2 | bugfix | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 1/0/1 | 27.09s/0.00s/27.09s | 27.95s | step_limit_reached |
| inherited_role_permissions | 2 | feature | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 1/0/1 | 27.55s/0.00s/27.55s | 28.45s | step_limit_reached |
| catalog_rename_cache_invalidation | 2 | refactor | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 56.65s/0.00s/56.65s | 57.57s | verifier_failed |
| notification_locale_fallback | 2 | bugfix | no_repo_map | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 26.25s/0.00s/26.25s | 27.09s | - |
| regional_checkout_shipping | 3 | bugfix | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 33.60s/0.00s/33.60s | 35.21s | - |
| tenant_scoped_webhook_dedup | 3 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 33.71s/0.00s/33.71s | 34.76s | - |
| inherited_role_permissions | 3 | feature | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 43.05s/0.00s/43.05s | 44.63s | - |
| catalog_rename_cache_invalidation | 3 | refactor | full | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 51.92s/0.00s/51.92s | 53.24s | verifier_failed |
| notification_locale_fallback | 3 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 33.71s/0.00s/33.71s | 35.27s | - |
| regional_checkout_shipping | 3 | bugfix | no_repo_map | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 32.77s/0.00s/32.77s | 33.64s | - |
| tenant_scoped_webhook_dedup | 3 | bugfix | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 1/0/1 | 26.08s/0.00s/26.08s | 26.98s | step_limit_reached |
| inherited_role_permissions | 3 | feature | no_repo_map | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 33.91s/0.00s/33.91s | 34.79s | - |
| catalog_rename_cache_invalidation | 3 | refactor | no_repo_map | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 62.45s/0.00s/62.45s | 63.37s | verifier_failed |
| notification_locale_fallback | 3 | bugfix | no_repo_map | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 36.79s/0.00s/36.79s | 37.63s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- In schema v3, compatibility fields for model calls, tokens, failures, rejections, and protocols cover the parent plus related delegates; explicit P/D/T fields retain the breakdown.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
