# Metrics evidence map

This directory separates the current evaluation claim from historical engineering
evidence. Reports are kept with their original measurements and scope boundaries;
an archived report is not a current benchmark claim.

## Current evidence

1. [`real-world-benchmark-v3-first-3x.md`](real-world-benchmark-v3-first-3x.md) —
   primary published result for commit `0897195`: 13/15 attempts across three
   clean-worktree repetitions; four of five tasks passed 3/3. It is not validation
   of later runtime revisions.
2. [`real-world-benchmark-v3-constraint-regression-3x.md`](real-world-benchmark-v3-constraint-regression-3x.md) —
   negative follow-up: a prompt change regressed the same frozen suite to 12/15 and
   was rolled back.
3. [`evaluation-methodology.md`](evaluation-methodology.md) — snapshot identity,
   repetition semantics, hidden-verifier isolation, delegation cost accounting, and
   interpretation limits.

The corresponding reviewed JSON artifacts remain under `artifacts/`. The benchmark
fixtures and hidden verifiers remain under `benchmarks/`.

## Historical / archive evidence

V1, V2, and the legacy structured-action comparison predate the current provenance
schema or have already entered the development feedback loop. They remain useful for
showing project evolution, but they are not the headline result and must not be read
as a strict causal ablation or a fresh held-out claim.

Use the [`archive` index](archive/README.md) for those reports and their limitations.
