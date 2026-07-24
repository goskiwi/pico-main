# pico-repo-map-localization-v4-frozen

- Captured at: `2026-07-24T02:31:38.499433Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `f69cb8e8319f7ef00ff38fb9df17320821321e9a`
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

| Variant | Map cap | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | dynamic | responses_function | 93.3% | 14/15 | 8.07 | 9.07/0.00/9.07 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 440786/0/440786 | 238208/0/238208 | 19216/0/19216 | 45.04s/0.00s/45.04s | 46.54s |
| repo_map_600 | 600 | responses_function | 100.0% | 15/15 | 7.60 | 8.60/0.00/8.60 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 399131/0/399131 | 224384/0/224384 | 17074/0/17074 | 43.80s/0.00s/43.80s | 45.34s |

## Repetition stability

| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |
|---|---:|---:|---:|---:|---:|
| full | 93.3% | 9.4% | 80.0% | 100.0% | 2/3 |
| repo_map_600 | 100.0% | 0.0% | 100.0% | 100.0% | 3/3 |

### Per repetition

| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |
|---|---:|---:|---:|---:|---:|
| full | 1 | 100.0% | 5/5 | 9.00 | 49.43s |
| full | 2 | 100.0% | 5/5 | 9.20 | 48.72s |
| full | 3 | 80.0% | 4/5 | 9.00 | 41.47s |
| repo_map_600 | 1 | 100.0% | 5/5 | 8.80 | 42.39s |
| repo_map_600 | 2 | 100.0% | 5/5 | 8.60 | 52.40s |
| repo_map_600 | 3 | 100.0% | 5/5 | 8.40 | 41.24s |

### Per-task stability

| Variant | Task | Pass rate | Passed | Outcome |
|---|---|---:|---:|---|
| full | catalog_rename_cache_invalidation | 66.7% | 2/3 | mixed |
| full | inherited_role_permissions | 100.0% | 3/3 | always_passed |
| full | notification_locale_fallback | 100.0% | 3/3 | always_passed |
| full | regional_checkout_shipping | 100.0% | 3/3 | always_passed |
| full | tenant_scoped_webhook_dedup | 100.0% | 3/3 | always_passed |
| repo_map_600 | catalog_rename_cache_invalidation | 100.0% | 3/3 | always_passed |
| repo_map_600 | inherited_role_permissions | 100.0% | 3/3 | always_passed |
| repo_map_600 | notification_locale_fallback | 100.0% | 3/3 | always_passed |
| repo_map_600 | regional_checkout_shipping | 100.0% | 3/3 | always_passed |
| repo_map_600 | tenant_scoped_webhook_dedup | 100.0% | 3/3 | always_passed |

## Ablation

- Pass-rate delta (full - repo_map_600): -6.7%
- Avg tool-step delta (full - repo_map_600): +0.47

## Failure breakdown

| Failure category | Count |
|---|---:|
| verifier_failed | 1 |

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| regional_checkout_shipping | 1 | bugfix | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 44.93s/0.00s/44.93s | 46.47s | - |
| tenant_scoped_webhook_dedup | 1 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 51.12s/0.00s/51.12s | 52.70s | - |
| inherited_role_permissions | 1 | feature | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 43.93s/0.00s/43.93s | 45.49s | - |
| catalog_rename_cache_invalidation | 1 | refactor | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 58.78s/0.00s/58.78s | 59.92s | - |
| notification_locale_fallback | 1 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 41.06s/0.00s/41.06s | 42.55s | - |
| regional_checkout_shipping | 1 | bugfix | repo_map_600 | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 23.31s/0.00s/23.31s | 24.80s | - |
| tenant_scoped_webhook_dedup | 1 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 50.68s/0.00s/50.68s | 52.30s | - |
| inherited_role_permissions | 1 | feature | repo_map_600 | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 43.54s/0.00s/43.54s | 45.09s | - |
| catalog_rename_cache_invalidation | 1 | refactor | repo_map_600 | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 55.21s/0.00s/55.21s | 56.82s | - |
| notification_locale_fallback | 1 | bugfix | repo_map_600 | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 31.37s/0.00s/31.37s | 32.92s | - |
| regional_checkout_shipping | 2 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 25.32s/0.00s/25.32s | 26.84s | - |
| tenant_scoped_webhook_dedup | 2 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 46.89s/0.00s/46.89s | 48.51s | - |
| inherited_role_permissions | 2 | feature | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 45.60s/0.00s/45.60s | 47.24s | - |
| catalog_rename_cache_invalidation | 2 | refactor | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 73.80s/0.00s/73.80s | 75.44s | - |
| notification_locale_fallback | 2 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 43.99s/0.00s/43.99s | 45.55s | - |
| regional_checkout_shipping | 2 | bugfix | repo_map_600 | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 33.14s/0.00s/33.14s | 34.68s | - |
| tenant_scoped_webhook_dedup | 2 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 50.57s/0.00s/50.57s | 52.21s | - |
| inherited_role_permissions | 2 | feature | repo_map_600 | PASS | PASS | 6 | 0 | 7/0/7 | 0/0/0 | 0/0/0 | 66.76s/0.00s/66.76s | 68.24s | - |
| catalog_rename_cache_invalidation | 2 | refactor | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 65.53s/0.00s/65.53s | 67.21s | - |
| notification_locale_fallback | 2 | bugfix | repo_map_600 | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 38.22s/0.00s/38.22s | 39.67s | - |
| regional_checkout_shipping | 3 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 31.96s/0.00s/31.96s | 33.45s | - |
| tenant_scoped_webhook_dedup | 3 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 44.95s/0.00s/44.95s | 46.55s | - |
| inherited_role_permissions | 3 | feature | full | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 42.65s/0.00s/42.65s | 44.15s | - |
| catalog_rename_cache_invalidation | 3 | refactor | full | FAIL | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 48.63s/0.00s/48.63s | 49.69s | verifier_failed |
| notification_locale_fallback | 3 | bugfix | full | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 31.98s/0.00s/31.98s | 33.51s | - |
| regional_checkout_shipping | 3 | bugfix | repo_map_600 | PASS | PASS | 6 | 0 | 7/0/7 | 0/0/0 | 0/0/0 | 21.21s/0.00s/21.21s | 22.75s | - |
| tenant_scoped_webhook_dedup | 3 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 58.23s/0.00s/58.23s | 59.85s | - |
| inherited_role_permissions | 3 | feature | repo_map_600 | PASS | PASS | 6 | 0 | 7/0/7 | 0/0/0 | 0/0/0 | 28.28s/0.00s/28.28s | 29.73s | - |
| catalog_rename_cache_invalidation | 3 | refactor | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 58.63s/0.00s/58.63s | 60.17s | - |
| notification_locale_fallback | 3 | bugfix | repo_map_600 | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 32.28s/0.00s/32.28s | 33.69s | - |

## Study interpretation

- `repo_map_600` passed 15/15 attempts and all three complete repetitions; dynamic
  `full` passed 14/15 and two of three complete repetitions.
- The only failed attempt was `full` on `catalog_rename_cache_invalidation`. The
  agent returned an explicitly incomplete implementation, and the hidden verifier
  rejected it. Neither variant recorded a model failure or action rejection.
- Relative to `full`, the 600-token cap used 9.5% fewer input tokens, 11.1% fewer
  output tokens, 5.8% fewer tool steps, 5.1% fewer model calls, and 2.6% less average
  wall time. Reported input-plus-output tokens per passing attempt fell from
  32,857 to 27,747, a 15.6% reduction.
- The 600-token candidate was chosen using a preceding V4 screen. This confirmation
  is therefore tuning/regression evidence on V4, not a new held-out result. The
  runtime default remains dynamic until an unseen localization suite validates the
  cap.

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- In schema v3, compatibility fields for model calls, tokens, failures, rejections, and protocols cover the parent plus related delegates; explicit P/D/T fields retain the breakdown.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
