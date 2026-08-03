# Pico runtime-contract benchmark V1 protocol

This suite turns deterministic regression coverage into a reviewable evidence
artifact. It is intentionally separate from Pico's live-model benchmarks:
passing it establishes a runtime contract for a frozen source snapshot, not
general coding-agent capability.

## Question

Can four harness mechanisms behave correctly and repeatably under controlled,
paired inputs?

1. Context reduction keeps the current request intact while bringing an
   over-budget prompt below its token limit.
2. The unchanged read guard avoids a physical duplicate read, replays saved
   evidence, and permits a fresh read after a workspace write.
3. Resume accepts valid checkpoints and distinguishes file freshness, runtime
   identity, and schema drift without selecting delegate or corrupt sessions.
4. Tool execution distinguishes a non-mutating non-zero shell exit from a
   non-zero exit that has already changed the workspace.

## Frozen design

- Manifest: [`benchmarks/runtime_contract_tasks_v1.json`](../../benchmarks/runtime_contract_tasks_v1.json)
- Runner: `scripts/run_runtime_contract_benchmark.py`
- Runtime implementation and per-row verifier:
  `pico/evaluation/runtime_contract_benchmark.py`
- Execution mode: `deterministic_scripted`
- Remote model calls: none
- Repetitions: three fresh workspaces per task by default

Each task declares its control, candidate/perturbation, and acceptance checks
before execution. The runner writes one JSON row for every task/repetition with
the expected/actual verifier checks, a source snapshot digest, and an outcome
fingerprint. Generated timestamps and session identifiers are normalized before
repeatability fingerprints are compared. An exception fails that row but does
not suppress the rest of the artifact.

## Paired controls

| Task | Control | Candidate / perturbation | Primary observations |
|---|---|---|---|
| `ctx_budget_preserves_current_request` | context reduction disabled | context reduction enabled | prompt tokens, request integrity, reductions |
| `memory_deduplicates_unchanged_read` | read-only dedup disabled | read-only dedup enabled | physical reads, cached evidence, fresh re-read |
| `resume_validates_checkpoint_freshness` | unchanged checkpoint | file/runtime/schema/session perturbations | resume state, stale paths, mismatch fields |
| `tool_classifies_mutating_failure` | non-mutating non-zero exit | mutating non-zero exit | status, affected paths, diff, process note |

The `read_only_dedup` feature flag exists solely so that this comparison has an
explicit control. Memory remains enabled in both sides; disabling memory would
not disable the independent duplicate-read guard.

## Reproduce

For local iteration (the artifact will truthfully record a dirty worktree if
applicable):

```bash
uv run python scripts/run_runtime_contract_benchmark.py \
  --repetitions 3 \
  --artifact-path artifacts/runtime-contract-benchmark-v1-3x.json \
  --report-path artifacts/runtime-contract-benchmark-v1-3x.md
```

For a reviewed result that can be committed alongside the report, run from a
clean worktree:

```bash
uv run python scripts/run_runtime_contract_benchmark.py \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path benchmarks/results/runtime-contract-benchmark-v1-3x.json \
  --report-path docs/metrics/runtime-contract-benchmark-v1-3x.md
```

## Interpretation boundary

The suite can support wording such as “four frozen runtime-contract tasks
passed across three deterministic isolated repetitions.” It cannot support
claims about live-model task success, token/cost savings on real repositories,
cross-session long-term memory retrieval, or a general-purpose benchmark rank.
