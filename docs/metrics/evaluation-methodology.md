# Real-model evaluation methodology

> **Status: current methodology.** Result status and archive boundaries are indexed
> in the [metrics evidence map](README.md).

Pico's real-world benchmark measures whether a model-driven agent can complete small repository
tasks that are accepted by tests unavailable to the agent during execution. It is an engineering
regression suite, not a claim about general coding ability.

The benchmark runner has no fake-model or offline execution path. A run requires a configured
remote LLM provider and records `execution_mode=live_llm`; missing API credentials fail preflight.

## Reproducibility controls

- Every attempt starts from a fresh copy of its fixture repository.
- Hidden verifier files are copied in only after the agent stops.
- Verification runs in Pico's network-disabled Docker sandbox.
- `evaluation_snapshot_id` hashes task prompts, tool and step limits, fixture contents, verifier
  commands, and hidden verifier contents. Comparisons reject mismatched snapshots.
- Artifacts record the provider, model label, Git commit, dirty-worktree state, Python and platform
  versions, sandbox limits, temperature, token limit, and verifier timeout.
- Published runs should use `--require-clean-worktree` so an artifact cannot silently describe
  uncommitted benchmark or runtime code.

## Repeated-run protocol

Use at least three full-suite repetitions when making a stability claim:

```bash
uv run python scripts/run_real_world_benchmark.py \
  --benchmark-path benchmarks/real_world_tasks_v2.json \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path artifacts/real-world-benchmark-v2-3x.json \
  --report-path docs/metrics/real-world-benchmark-v2-3x.md
```

The benchmark uses only `OPENAI_API_BASE`, `OPENAI_API_KEY`, and `OPENAI_MODEL`
from the repository `.env.local`; `--model` and `--base-url` are one-run overrides.

The separate `benchmarks/real_world_tasks_delegate.json` suite is an observed
live integration regression for delegation, not a held-out capability claim. It
requires one `delegate_many` attempt with two requested children, structured
successful outcome metadata, two matching related child-run identities, and a
passing hidden verifier. Missing, rejected, partial, malformed, or count-mismatched
evidence fails closed.

## Delegation cost accounting

Artifact schema v3 accounts for every model call present in the parent and
related delegate traces when an attempt is aggregated. Attempts that require
successful delegation fail closed when the expected child runs are absent or
do not complete; a timed-out Python thread cannot be forcibly terminated, so a
late call written after aggregation is deliberately not presented as completed-attempt cost.
Rows expose `parent_*`, `delegate_*`, and `total_*` fields for model calls, input
tokens, output tokens, cached tokens, and cumulative model-call duration. The
compatibility fields `model_calls`, `input_tokens`, `output_tokens`, and
`cached_tokens` are aliases for the corresponding v3 totals. For a task that
does not delegate, delegate values are zero and the compatibility values remain
identical to the parent values.

The same P/D/T accounting applies to `model_failures` and
`model_action_rejections`; their compatibility fields are totals. Action
protocols are reported as `parent_action_protocols`,
`delegate_action_protocols`, and their union in both `total_action_protocols`
and `action_protocols`. Tool-policy evidence remains intentionally parent-only:
`executed_tools`, `required_tools`, and `missing_required_tools` are evaluated
from the parent trace. Delegate success uses the parent's structured outcome
metadata, then cross-checks the reported child IDs and counts against the
related run traces; it never parses the clipped human-readable tool preview.

Delegate totals include only run directories created during the current
attempt whose `run_started` identity forms a parent-agent chain from the
attempt's explicit parent run. Every included run must be an immediate child of
the attempt's `RunStore` directory and record the same workspace root. This
excludes historical runs, unrelated concurrent runs, and runs outside the
benchmark workspace. `delegate_run_count` and `delegate_run_ids` retain the
membership evidence.

Summary rows expose both `avg_delegate_run_count` and
`total_delegate_run_count`; Results tables use the average so it is comparable
to the adjacent per-attempt averages. The legacy summary key
`delegate_run_count` remains a total-count alias.

`parent_model_duration_ms`, `delegate_model_duration_ms`, and
`total_model_duration_ms` sum individual model-call durations. They measure
model workload, not end-to-end latency: concurrent delegate calls can overlap.
`agent_duration_ms` remains the parent attempt's wall-clock duration and already
includes time spent waiting for delegates; `total_duration_ms` adds hidden
verification time.

Schema v1/v2 artifacts used parent-only compatibility fields. The report
renderer continues to accept them and labels their cost scope as
`parent_run_only`. Cost comparisons reject artifacts with different scopes, so
a parent-only artifact cannot be presented as directly comparable to a schema
v3 parent-plus-delegate artifact.

The generated report includes:

- pooled pass rate over all attempts;
- mean, population standard deviation, minimum, and maximum full-suite pass rate;
- the number of completely successful repetitions;
- per-repetition cost and latency indicators;
- per-task outcomes (`always_passed`, `mixed`, or `always_failed`);
- failure-category counts and raw attempt rows.

Repeated attempts over the same task are not independent task samples. The reported standard
deviation describes run-to-run stability on this fixed suite; it is not a confidence interval for
unseen repositories.

## Interpretation boundary

- A model label may point to a provider implementation that changes over time.
- Temperature zero reduces randomness but does not guarantee deterministic remote inference.
- The held-out suite protects against tuning only if it remains unobserved during development.
- Once failures from a suite have informed runtime changes, that suite becomes a regression suite;
  new held-out claims require previously unobserved tasks.
- Pass rate does not measure maintainability, user preference, or safety outside the verifier scope.
- Any benchmark claim should link its JSON artifact and rendered Markdown report.
