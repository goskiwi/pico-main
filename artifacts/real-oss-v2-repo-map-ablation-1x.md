# pico-real-oss-v2

- Captured at: `2026-07-26T14:51:44.120940Z`
- Provider: `openai`
- Model: `gpt-5.6-luna`
- Execution mode: `live_llm`
- Commit: `3600c1d5e6fa82e9acaec573cfb2e617ac6d4fe7`
- Working tree dirty: `False`
- Tasks: 10
- Repetitions: 1
- Fixture snapshot: `sha256:62f2d7de58b8574ddebb36d3c5c34f75436216fed2debc5d63efec3153e301e7`
- Evaluation snapshot: `sha256:2e1cdd307f583e11b83936355715078564246352256a56b6642dba9c613fbc3c`
- Run config: temperature=0.0, max_new_tokens=1024, verifier_timeout=90s
- Model cost scope: `attempt_parent_and_related_delegates`
- Duration semantics: model time is cumulative across model calls; agent duration is parent-attempt wall time and already includes delegate wait
- Sandbox: `pico-real-oss-v2:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Map cap | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | dynamic | responses_function | 100.0% | 10/10 | 14.30 | 15.30/0.00/15.30 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 1361682/0/1361682 | 390784/0/390784 | 12940/0/12940 | 69.79s/0.00s/69.79s | 75.57s |
| no_repo_map | disabled | responses_function | 100.0% | 10/10 | 16.90 | 17.90/0.00/17.90 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 1416654/0/1416654 | 335232/0/335232 | 12350/0/12350 | 78.88s/0.00s/78.88s | 83.22s |

## Ablation

- Pass-rate delta (full - no_repo_map): +0.0%
- Avg tool-step delta (full - no_repo_map): -2.60

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| pydantic_slots_forwarded_generic | 1 | bugfix | full | PASS | PASS | 16 | 0 | 17/0/17 | 0/0/0 | 0/0/0 | 72.44s/0.00s/72.44s | 82.84s | - |
| pytest_indirect_parametrize | 1 | bugfix | full | PASS | PASS | 21 | 0 | 22/0/22 | 0/0/0 | 0/0/0 | 96.41s/0.00s/96.41s | 105.61s | - |
| click_empty_bytes_echo | 1 | bugfix | full | PASS | PASS | 11 | 0 | 12/0/12 | 0/0/0 | 0/0/0 | 76.40s/0.00s/76.40s | 84.05s | - |
| tomlkit_float_not_sequence | 1 | bugfix | full | PASS | PASS | 14 | 0 | 15/0/15 | 0/0/0 | 0/0/0 | 58.83s/0.00s/58.83s | 63.15s | - |
| tqdm_infinite_total_format_meter | 1 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 38.43s/0.00s/38.43s | 41.81s | - |
| packaging_non_string_version | 1 | bugfix | full | PASS | PASS | 17 | 0 | 18/0/18 | 0/0/0 | 0/0/0 | 84.40s/0.00s/84.40s | 88.98s | - |
| werkzeug_float_url_notation | 1 | bugfix | full | PASS | PASS | 15 | 0 | 16/0/16 | 0/0/0 | 0/0/0 | 87.30s/0.00s/87.30s | 91.11s | - |
| more_itertools_negative_tail | 1 | bugfix | full | PASS | PASS | 4 | 0 | 5/0/5 | 0/0/0 | 0/0/0 | 29.20s/0.00s/29.20s | 36.24s | - |
| jinja_overlay_async_default | 1 | bugfix | full | PASS | PASS | 12 | 0 | 13/0/13 | 0/0/0 | 0/0/0 | 46.85s/0.00s/46.85s | 50.16s | - |
| urllib3_port_zero | 1 | bugfix | full | PASS | PASS | 24 | 0 | 25/0/25 | 0/0/0 | 0/0/0 | 107.60s/0.00s/107.60s | 111.78s | - |
| pydantic_slots_forwarded_generic | 1 | bugfix | no_repo_map | PASS | PASS | 16 | 0 | 17/0/17 | 0/0/0 | 0/0/0 | 70.57s/0.00s/70.57s | 76.89s | - |
| pytest_indirect_parametrize | 1 | bugfix | no_repo_map | PASS | PASS | 24 | 0 | 25/0/25 | 0/0/0 | 0/0/0 | 103.16s/0.00s/103.16s | 107.29s | - |
| click_empty_bytes_echo | 1 | bugfix | no_repo_map | PASS | PASS | 12 | 0 | 13/0/13 | 0/0/0 | 0/0/0 | 100.25s/0.00s/100.25s | 107.80s | - |
| tomlkit_float_not_sequence | 1 | bugfix | no_repo_map | PASS | PASS | 20 | 0 | 21/0/21 | 0/0/0 | 0/0/0 | 116.12s/0.00s/116.12s | 118.48s | - |
| tqdm_infinite_total_format_meter | 1 | bugfix | no_repo_map | PASS | PASS | 14 | 0 | 15/0/15 | 0/0/0 | 0/0/0 | 50.08s/0.00s/50.08s | 53.07s | - |
| packaging_non_string_version | 1 | bugfix | no_repo_map | PASS | PASS | 20 | 0 | 21/0/21 | 0/0/0 | 0/0/0 | 85.87s/0.00s/85.87s | 89.06s | - |
| werkzeug_float_url_notation | 1 | bugfix | no_repo_map | PASS | PASS | 14 | 0 | 15/0/15 | 0/0/0 | 0/0/0 | 68.46s/0.00s/68.46s | 71.15s | - |
| more_itertools_negative_tail | 1 | bugfix | no_repo_map | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 38.52s/0.00s/38.52s | 45.59s | - |
| jinja_overlay_async_default | 1 | bugfix | no_repo_map | PASS | PASS | 16 | 0 | 17/0/17 | 0/0/0 | 0/0/0 | 63.03s/0.00s/63.03s | 66.25s | - |
| urllib3_port_zero | 1 | bugfix | no_repo_map | PASS | PASS | 24 | 0 | 25/0/25 | 0/0/0 | 0/0/0 | 92.77s/0.00s/92.77s | 96.66s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- Model calls, tokens, failures, rejections, protocols, and duration are reported explicitly for parent, delegates, and their total.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
