# Metrics evidence map

This directory separates the current evaluation claim from historical engineering
evidence. Reports are kept with their original measurements and scope boundaries;
an archived report is not a current benchmark claim.

## Current evidence

1. [`reliability-benchmark-v1-live-3x.md`](reliability-benchmark-v1-live-3x.md) —
   first execution of the frozen
   [reliability protocol](reliability-benchmark-v1-protocol.md) from clean commit
   `5d80ce5`. The cross-module Repo Map task passed 3/3, both Undo scenarios
   recovered 6/6, complete post-Undo workspace digests matched their pre-run
   digests 6/6, and the pre-existing dirty README was preserved 3/3. The nine
   attempts recorded no model failures, Action rejections, trace parse errors, or
   workspace-isolation failures. This is a small engineering regression, not a new
   Repo Map ablation or a general coding-capability result.
2. [`progress-feedback-live-ab-3x.md`](progress-feedback-live-ab-3x.md) —
   focused live `gpt-5.4` A/B over clean, snapshot-matched runtimes. The candidate
   passed 6/6 attempts versus 2/6 for the baseline. Pytest tail localization moved
   from 0/3 to 3/3; the artificial stagnation-recovery mechanism moved from 2/3 to
   3/3 and used fewer model calls. This is targeted engineering evidence, not a
   general coding benchmark.
3. [`real-world-benchmark-v5-repo-map-budget-600-vs-full-first-3x.md`](real-world-benchmark-v5-repo-map-budget-600-vs-full-first-3x.md) —
   first live-model result over the new V5 `ops_center` suite, frozen with its
   [decision protocol](v5-repo-map-budget-decision-protocol.md) at clean commit
   `363a8e8`. Both dynamic `full` and the 600-token hard cap passed 14/15 attempts.
   The cap reduced reported input-plus-output tokens per passing attempt by 3.36%,
   below the pre-registered 5% default-change threshold, so the dynamic default was
   retained. Neither variant recorded model failures, action rejections, or
   isolation failures. V5 became a regression suite after this result was inspected.
4. [`real-world-benchmark-v4-repo-map-budget-600-vs-full-3x.md`](real-world-benchmark-v4-repo-map-budget-600-vs-full-3x.md) —
   budget-tuning confirmation for clean commit `f69cb8e`: the 600-token cap passed
   15/15 attempts and dynamic `full` passed 14/15. The cap used 9.5% fewer input
   tokens, 11.1% fewer output tokens, and 15.6% fewer reported input-plus-output
   tokens per passing attempt. This is confirmation on the V4 tuning/regression
   suite, not a new held-out result, so it does not by itself justify changing the
   runtime default. The preceding
   [`single-repetition screen`](real-world-benchmark-v4-repo-map-budget-screen-1x.md)
   selected 600 by the declared pass-rate-first rule, but both competing failures
   were remote `model_error` events rather than verifier failures.
5. [`real-world-benchmark-v4-repo-map-ablation-3x.md`](real-world-benchmark-v4-repo-map-ablation-3x.md) —
   primary localization result for clean commit `016c618`: `full` passed 13/15
   attempts and `no_repo_map` passed 6/15, an observed +46.7 percentage-point
   difference on the frozen five-task suite. `full` used 33.8% more tokens per
   attempt and 22.1% more wall time, but 38.3% fewer tokens per passing attempt.
   This is a targeted Repo Map benchmark, not a universal coding-agent claim.
6. [`real-world-benchmark-v3-first-3x.md`](real-world-benchmark-v3-first-3x.md) —
   primary published result for commit `0897195`: 13/15 attempts across three
   clean-worktree repetitions; four of five tasks passed 3/3. It is not validation
   of later runtime revisions.
7. [`real-world-benchmark-v3-constraint-regression-3x.md`](real-world-benchmark-v3-constraint-regression-3x.md) —
   negative follow-up: a prompt change regressed the same frozen suite to 12/15 and
   was rolled back.
8. [`evaluation-methodology.md`](evaluation-methodology.md) — snapshot identity,
   repetition semantics, hidden-verifier isolation, delegation cost accounting, and
   interpretation limits.

The corresponding reviewed JSON artifacts remain under `artifacts/`. The benchmark
fixtures and hidden verifiers remain under `benchmarks/`.

## Frozen protocols accompanying results

1. [`reliability-benchmark-v1-protocol.md`](reliability-benchmark-v1-protocol.md) —
   pre-registers three live-model scenarios and nine attempts covering a
   cross-module Repo Map task, exact Undo restoration after a rejected two-file
   change, and preservation of a pre-existing dirty README modified again by the
   agent. The protocol requires publishing failures without editing the frozen
   tasks or acceptance gates.

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
