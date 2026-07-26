# pico-real-oss-v1-smoke

- Captured at: `2026-07-26T07:58:36.160227Z`
- Provider: `openai`
- Model: `gpt-5.6-luna`
- Execution mode: `live_llm`
- Commit: `622a79708b3b342e5c2c3c9b8da0bddb8c9bf561`
- Working tree dirty: `False`
- Tasks: 3
- Repetitions: 1
- Fixture snapshot: `sha256:142ae81b607a0b613a5eac4d9731d21b8ee3470adf1c9bd175c33deb993c414a`
- Evaluation snapshot: `sha256:5e44634950cf91a2cc9f79d4abcf440da7dfb506352632dc25dcaa061fc01127`
- Run config: temperature=0.0, max_new_tokens=1024, verifier_timeout=90s
- Model cost scope: `attempt_parent_and_related_delegates`
- Duration semantics: model time is cumulative across model calls; agent duration is parent-attempt wall time and already includes delegate wait
- Sandbox: `pico-real-oss-v1:latest`, 4.0 CPU, 4g memory, 512 PIDs

## Results

| Variant | Map cap | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full | dynamic | responses_function | 100.0% | 3/3 | 12.33 | 13.33/0.00/13.33 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 325109/0/325109 | 118144/0/118144 | 5031/0/5031 | 155.76s/0.00s/155.76s | 164.21s |
| no_repo_map | disabled | responses_function | 100.0% | 3/3 | 19.00 | 20.00/0.00/20.00 | 0.00 | 0.00/0.00/0.00 | 0.00/0.00/0.00 | 472595/0/472595 | 210048/0/210048 | 6438/0/6438 | 82.74s/0.00s/82.74s | 89.04s |

## Ablation

- Pass-rate delta (full - no_repo_map): +0.0%
- Avg tool-step delta (full - no_repo_map): -6.67

## Task details

| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |
|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| pydantic_slots_forwarded_generic | 1 | bugfix | full | PASS | PASS | 9 | 0 | 10/0/10 | 0/0/0 | 0/0/0 | 323.15s/0.00s/323.15s | 333.50s | - |
| pytest_indirect_parametrize | 1 | bugfix | full | PASS | PASS | 18 | 0 | 19/0/19 | 0/0/0 | 0/0/0 | 98.82s/0.00s/98.82s | 106.22s | - |
| click_empty_bytes_echo | 1 | bugfix | full | PASS | PASS | 10 | 0 | 11/0/11 | 0/0/0 | 0/0/0 | 45.32s/0.00s/45.32s | 52.90s | - |
| pydantic_slots_forwarded_generic | 1 | bugfix | no_repo_map | PASS | PASS | 16 | 0 | 17/0/17 | 0/0/0 | 0/0/0 | 67.72s/0.00s/67.72s | 74.60s | - |
| pytest_indirect_parametrize | 1 | bugfix | no_repo_map | PASS | PASS | 24 | 0 | 25/0/25 | 0/0/0 | 0/0/0 | 105.82s/0.00s/105.82s | 109.87s | - |
| click_empty_bytes_echo | 1 | bugfix | no_repo_map | PASS | PASS | 17 | 0 | 18/0/18 | 0/0/0 | 0/0/0 | 74.69s/0.00s/74.69s | 82.63s | - |

## Scope boundary

- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.
- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.
- Model calls, tokens, failures, rejections, protocols, and duration are reported explicitly for parent, delegates, and their total.
- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.
- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.
- Verifiers run inside the mandatory Docker sandbox with networking disabled.
- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.
- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.
