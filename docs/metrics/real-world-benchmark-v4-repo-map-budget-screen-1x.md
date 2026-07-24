# pico-repo-map-localization-v4-frozen

- Captured at: `2026-07-24T00:53:04.563667Z`
- Provider: `openai`
- Model: `gpt-5.4`
- Execution mode: `live_llm`
- Commit: `f69cb8e8319f7ef00ff38fb9df17320821321e9a`
- Working tree dirty: `False`
- Tasks: 5
- Repetitions: 1
- Fixture snapshot: `sha256:3417db101b69581cef16e87129dc4d1d648b97cc283e28887afddfae3c5137dd`
- Evaluation snapshot: `sha256:7f0029bc8bf8779432f1dc72fd903fac3ab07521c68aad08924c08e56d6e7824`
- Run config: temperature=0.0, max_new_tokens=1024, verifier_timeout=90s
- Model cost scope: `attempt_parent_and_related_delegates`
- Duration semantics: model time is cumulative across model calls; agent duration is parent-attempt wall time and already includes delegate wait
- Sandbox: `pico-sandbox:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Map cap | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| repo_map_600 | 600 | responses_function | 100.0% | 5/5 | 8.20 | 9.20/0.00/9.20 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 146522/0/146522 | 64128/0/64128 | 6344/0/6344 | 41.03s/0.00s/41.03s | 42.46s |
| repo_map_1000 | 1000 | responses_function | 80.0% | 4/5 | 6.40 | 7.40/0.00/7.40 | 0.00 | 0.20/0.00/0.20 | 0.00/0.00/0.00 | 113002/0/113002 | 38912/0/38912 | 4266/0/4266 | 46.94s/0.00s/46.94s | 48.16s |
| repo_map_1600 | 1600 | responses_function | 80.0% | 4/5 | 6.80 | 7.80/0.00/7.80 | 0.00 | 0.20/0.00/0.20 | 0.00/0.00/0.00 | 118040/0/118040 | 51072/0/51072 | 4673/0/4673 | 92.35s/0.00s/92.35s | 93.50s |

## Failure breakdown

| Failure category | Count |
|---|---:|
| model_error | 2 |

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| regional_checkout_shipping | 1 | bugfix | repo_map_600 | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 29.42s/0.00s/29.42s | 30.97s | - |
| tenant_scoped_webhook_dedup | 1 | bugfix | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 44.04s/0.00s/44.04s | 45.05s | - |
| inherited_role_permissions | 1 | feature | repo_map_600 | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 46.39s/0.00s/46.39s | 47.88s | - |
| catalog_rename_cache_invalidation | 1 | refactor | repo_map_600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 56.73s/0.00s/56.73s | 58.32s | - |
| notification_locale_fallback | 1 | bugfix | repo_map_600 | PASS | PASS | 7 | 0 | 8/0/8 | 0/0/0 | 0/0/0 | 28.59s/0.00s/28.59s | 30.06s | - |
| regional_checkout_shipping | 1 | bugfix | repo_map_1000 | PASS | PASS | 6 | 0 | 7/0/7 | 0/0/0 | 0/0/0 | 26.87s/0.00s/26.87s | 28.29s | - |
| tenant_scoped_webhook_dedup | 1 | bugfix | repo_map_1000 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 64.04s/0.00s/64.04s | 65.66s | - |
| inherited_role_permissions | 1 | feature | repo_map_1000 | PASS | PASS | 6 | 0 | 7/0/7 | 0/0/0 | 0/0/0 | 31.33s/0.00s/31.33s | 32.74s | - |
| catalog_rename_cache_invalidation | 1 | refactor | repo_map_1000 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 50.25s/0.00s/50.25s | 51.24s | - |
| notification_locale_fallback | 1 | bugfix | repo_map_1000 | FAIL | PASS | 2 | 0 | 3/0/3 | 1/0/1 | 0/0/0 | 62.21s/0.00s/62.21s | 62.86s | model_error |
| regional_checkout_shipping | 1 | bugfix | repo_map_1600 | FAIL | PASS | 0 | 0 | 1/0/1 | 1/0/1 | 0/0/0 | 12.31s/0.00s/12.31s | 12.91s | model_error |
| tenant_scoped_webhook_dedup | 1 | bugfix | repo_map_1600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 331.09s/0.00s/331.09s | 332.45s | - |
| inherited_role_permissions | 1 | feature | repo_map_1600 | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 24.93s/0.00s/24.93s | 26.58s | - |
| catalog_rename_cache_invalidation | 1 | refactor | repo_map_1600 | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 63.75s/0.00s/63.75s | 64.88s | - |
| notification_locale_fallback | 1 | bugfix | repo_map_1600 | PASS | PASS | 8 | 0 | 9/0/9 | 0/0/0 | 0/0/0 | 29.67s/0.00s/29.67s | 30.68s | - |

## Study interpretation

- The declared selection rule was pass rate first, then lower cost. It selected
  `repo_map_600` at 5/5; `repo_map_1000` and `repo_map_1600` each passed 4/5.
- Both competing failures were remote `model_error` events, not hidden-verifier
  failures. This screen therefore selected a confirmation candidate but did not
  establish that 600 tokens is semantically better than 1000 or 1600.
- V4 had already entered the development feedback loop. These results are tuning
  evidence on a fixed regression suite, not a held-out generalization result.

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- In schema v3, compatibility fields for model calls, tokens, failures, rejections, and protocols cover the parent plus related delegates; explicit P/D/T fields retain the breakdown.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
