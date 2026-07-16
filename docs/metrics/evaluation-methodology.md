# Real-model evaluation methodology

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
  --provider openai \
  --model YOUR_MODEL \
  --benchmark-path benchmarks/real_world_tasks_v2.json \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path artifacts/real-world-benchmark-v2-3x.json \
  --report-path docs/metrics/real-world-benchmark-v2-3x.md
```

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
