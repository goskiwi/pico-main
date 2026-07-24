# Metrics evidence map

This directory separates the current evaluation claim from historical engineering
evidence. Reports are kept with their original measurements and scope boundaries;
an archived report is not a current benchmark claim.

## Current evidence

1. [`real-world-benchmark-v4-repo-map-budget-600-vs-full-3x.md`](real-world-benchmark-v4-repo-map-budget-600-vs-full-3x.md) —
   budget-tuning confirmation for clean commit `f69cb8e`: the 600-token cap passed
   15/15 attempts and dynamic `full` passed 14/15. The cap used 9.5% fewer input
   tokens, 11.1% fewer output tokens, and 15.6% fewer reported input-plus-output
   tokens per passing attempt. This is confirmation on the V4 tuning/regression
   suite, not a new held-out result, so it does not by itself justify changing the
   runtime default. The preceding
   [`single-repetition screen`](real-world-benchmark-v4-repo-map-budget-screen-1x.md)
   selected 600 by the declared pass-rate-first rule, but both competing failures
   were remote `model_error` events rather than verifier failures.
2. [`real-world-benchmark-v4-repo-map-ablation-3x.md`](real-world-benchmark-v4-repo-map-ablation-3x.md) —
   primary localization result for clean commit `016c618`: `full` passed 13/15
   attempts and `no_repo_map` passed 6/15, an observed +46.7 percentage-point
   difference on the frozen five-task suite. `full` used 33.8% more tokens per
   attempt and 22.1% more wall time, but 38.3% fewer tokens per passing attempt.
   This is a targeted Repo Map benchmark, not a universal coding-agent claim.
3. [`real-world-benchmark-v3-first-3x.md`](real-world-benchmark-v3-first-3x.md) —
   primary published result for commit `0897195`: 13/15 attempts across three
   clean-worktree repetitions; four of five tasks passed 3/3. It is not validation
   of later runtime revisions.
4. [`real-world-benchmark-v3-constraint-regression-3x.md`](real-world-benchmark-v3-constraint-regression-3x.md) —
   negative follow-up: a prompt change regressed the same frozen suite to 12/15 and
   was rolled back.
5. [`evaluation-methodology.md`](evaluation-methodology.md) — snapshot identity,
   repetition semantics, hidden-verifier isolation, delegation cost accounting, and
   interpretation limits.

The corresponding reviewed JSON artifacts remain under `artifacts/`. The benchmark
fixtures and hidden verifiers remain under `benchmarks/`.

## Development-only evidence

1. [`real-world-benchmark-v3-repo-map-ablation-live-1x.md`](real-world-benchmark-v3-repo-map-ablation-live-1x.md) —
   a single-repetition live A/B on the frozen V3 suite comparing `full` with
   `no_repo_map`. Both variants passed 4/5 tasks; `full` used 1.8 fewer tool steps
   per task on average. This run was captured from a dirty working tree, so it is
   directional regression evidence rather than a publishable benchmark claim.

## Historical / archive evidence

V1, V2, and the legacy structured-action comparison predate the current provenance
schema or have already entered the development feedback loop. They remain useful for
showing project evolution, but they are not the headline result and must not be read
as a strict causal ablation or a fresh held-out claim.

Use the [`archive` index](archive/README.md) for those reports and their limitations.
