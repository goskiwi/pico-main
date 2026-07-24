# Progress feedback live A/B

## Result

The candidate passed **6/6 (100.0%)** attempts; the baseline passed **2/6
(33.3%)**, an observed difference of **+66.7 percentage points** on this
two-task targeted suite.

| Scenario | Baseline | Candidate | Baseline avg calls | Candidate avg calls |
|---|---:|---:|---:|---:|
| `pytest_failure_tail_localization` | 0/3 | 3/3 | 3.00 | 3.00 |
| `stagnation_nudge_recovery` | 2/3 | 3/3 | 7.33 | 5.00 |
| **Overall** | **2/6** | **6/6** | **5.17** | **4.00** |

The pytest result is the strongest finding. All three baseline attempts saw only
the clipped noise prefix and invented a different nonexistent node id. All three
candidate attempts received the real failing node id from the compacted tail and
passed the hidden verifier.

The stagnation task is supporting mechanism evidence, not a natural-task success
estimate. The candidate emitted exactly one `progress_nudge` in each repetition
and passed 3/3. The baseline emitted no nudge, recorded 3/1/1 hard repeated-call
rejections, and still passed 2/3 because the model violated the task's instruction
and switched early. One baseline repetition exhausted the step budget.

## Cost and execution

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Input tokens | 54,354 | 43,726 | -19.6% |
| Output tokens | 1,028 | 717 | -30.3% |
| Model calls | 31 | 24 | -22.6% |
| Tool steps | 19 | 18 | -5.3% |
| Total measured duration | 90.80s | 69.36s | -23.6% |
| Model failures | 0 | 0 | — |
| Model Action rejections | 7 | 0 | -7 |

For the pytest task, each candidate run recorded a 7,913-character redacted raw
tool artifact and a 4,000-character model-facing summary. The summary surfaced
`FAILED tests/test_noisy_failure.py::test_tail_only_failure` before the retained
tail; the complete raw output remained under `tool_outputs/`.

For the stagnation task, the candidate used 21,097 input and 358 output tokens
across three attempts, versus 31,230 input and 709 output tokens for the baseline.

## Protocol

- Model: `gpt-5.4`; temperature 0; maximum 512 output tokens per model call.
- Three repetitions per scenario, `no_repo_map` variant, clean worktrees.
- Python 3.12.13 on both sides.
- Docker verifier: `pico-sandbox:latest`, 4 CPU, 4 GB memory, network disabled
  for tool and verifier execution.
- Baseline commit: `d5ad9b499d402604361c512513073e1cca56cfd9`
  (`master` plus the frozen benchmark only).
- Candidate commit: `71b30f768ca0a6e4333bd509d991aa8258dde554`.
- Evaluation snapshot:
  `sha256:8ebcc825694b14fd311d32b34ce47467c0d98376aa940c8e95ebe871e920fcca`.
- Fixture snapshot:
  `sha256:0e898db240d54c441a40fd608db5c7ff48a3123803590ae6932e8f2edab7ebe7`.

Raw reviewed artifacts:

- [baseline](../../artifacts/progress-feedback-live-ab-baseline-3x.json)
- [candidate](../../artifacts/progress-feedback-live-ab-candidate-3x.json)
- [reviewed comparison](../../artifacts/progress-feedback-live-ab-comparison.json)

## Validity and scope

The task manifest, fixture, and hidden verifiers were committed and pushed before
the first model request. The final baseline and candidate artifacts have matching
evaluation and fixture snapshot ids, matching model configuration, matching
Python versions, and clean worktrees.

A preliminary candidate execution was excluded because a manual fixture smoke
test had created ignored cache directories, producing a different evaluation
snapshot. Those generated caches were moved out of the fixture, snapshot equality
was verified, and the candidate was rerun from a fresh detached worktree. The
excluded run also passed 6/6 but is not used in the tables above.

This is a deliberately small engineering A/B. It demonstrates that pytest-aware
tail compaction causally improved this long-output localization task and that the
soft nudge reached the real model reliably. It does not establish an improvement
on general coding tasks or estimate how often natural workloads trigger the
stagnation detector.
