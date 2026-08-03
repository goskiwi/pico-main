# Pico runtime-contract benchmark V1

## Result

- Overall: **12/12 (100.0%)** isolated deterministic attempts satisfied every pre-registered verifier.
- Repetitions: **3** fresh workspace(s) per task; each task's normalized outcome fingerprint is listed below.
- Remote model calls: **0**. The action source is a versioned scripted client.

## Per-task repeatability

| Task | Family | Passed | Unique normalized outcome fingerprints |
|---|---|---:|---:|
| `ctx_budget_preserves_current_request` | context_management | 3/3 | 1 |
| `memory_deduplicates_unchanged_read` | working_memory | 3/3 | 1 |
| `resume_validates_checkpoint_freshness` | checkpoint_resume | 3/3 | 1 |
| `tool_classifies_mutating_failure` | tool_governance | 3/3 | 1 |

## Paired observations (first repetition)

- Context budget: control rendered 1220 tokens for a 220-token budget; candidate rendered 216 tokens, retained the full current request, and recorded 4 old-context reduction(s).
- Memory/read guard: the same scripted sequence performed 3 physical reads in the control and 2 in the candidate; the candidate replayed cached evidence once and re-read after the workspace write.
- Resume validation: controlled perturbations produced `partial-stale`, `workspace-mismatch`, and `schema-mismatch` respectively; latest-session selection also rejected delegate and corrupt entries.
- Tool outcome: a non-mutating failure was classified `error`; the same non-zero exit with a workspace write was classified `partial_success` with preserved diff evidence.

## Attempt evidence

| Task | Rep | Control | Candidate | Verifier | Outcome fingerprint |
|---|---:|---|---|---:|---|
| `ctx_budget_preserves_current_request` | 1 | context_reduction_disabled | context_reduction_enabled | PASS | `sha256:94e9bb12003fa42778b1615f2c0436739f15fb5194901cadd1ffb7e8ed3eef81` |
| `memory_deduplicates_unchanged_read` | 1 | read_only_dedup_disabled | read_only_dedup_enabled | PASS | `sha256:787a666dc94326f1698084716474facc77232a38c4d7c088f0618633b8f17955` |
| `resume_validates_checkpoint_freshness` | 1 | unchanged_checkpoint_resume | controlled_file_runtime_schema_and_session_perturbations | PASS | `sha256:808762777b8cf8139fa716a500f6a2321110cc6462c3279c815ad994c6e9717d` |
| `tool_classifies_mutating_failure` | 1 | non_mutating_nonzero_shell_exit | mutating_nonzero_shell_exit | PASS | `sha256:82969d484e4ca684b72006604d939374ebce37496b7f86b36f2d5626edcdb050` |
| `ctx_budget_preserves_current_request` | 2 | context_reduction_disabled | context_reduction_enabled | PASS | `sha256:94e9bb12003fa42778b1615f2c0436739f15fb5194901cadd1ffb7e8ed3eef81` |
| `memory_deduplicates_unchanged_read` | 2 | read_only_dedup_disabled | read_only_dedup_enabled | PASS | `sha256:787a666dc94326f1698084716474facc77232a38c4d7c088f0618633b8f17955` |
| `resume_validates_checkpoint_freshness` | 2 | unchanged_checkpoint_resume | controlled_file_runtime_schema_and_session_perturbations | PASS | `sha256:808762777b8cf8139fa716a500f6a2321110cc6462c3279c815ad994c6e9717d` |
| `tool_classifies_mutating_failure` | 2 | non_mutating_nonzero_shell_exit | mutating_nonzero_shell_exit | PASS | `sha256:82969d484e4ca684b72006604d939374ebce37496b7f86b36f2d5626edcdb050` |
| `ctx_budget_preserves_current_request` | 3 | context_reduction_disabled | context_reduction_enabled | PASS | `sha256:94e9bb12003fa42778b1615f2c0436739f15fb5194901cadd1ffb7e8ed3eef81` |
| `memory_deduplicates_unchanged_read` | 3 | read_only_dedup_disabled | read_only_dedup_enabled | PASS | `sha256:787a666dc94326f1698084716474facc77232a38c4d7c088f0618633b8f17955` |
| `resume_validates_checkpoint_freshness` | 3 | unchanged_checkpoint_resume | controlled_file_runtime_schema_and_session_perturbations | PASS | `sha256:808762777b8cf8139fa716a500f6a2321110cc6462c3279c815ad994c6e9717d` |
| `tool_classifies_mutating_failure` | 3 | non_mutating_nonzero_shell_exit | mutating_nonzero_shell_exit | PASS | `sha256:82969d484e4ca684b72006604d939374ebce37496b7f86b36f2d5626edcdb050` |

## Protocol

- Every attempt starts from a new, local workspace directory.
- Each task compares a fixed control with one runtime treatment or controlled perturbation.
- The row verifier records individual expected/actual checks in the JSON artifact.
- Outcome fingerprints normalize generated timestamps and session identifiers before comparison.
- The benchmark has no provider, network, or live-model dependency.

Reproduce from the repository root:

```bash
uv run python scripts/run_runtime_contract_benchmark.py \
  --repetitions 3 \
  --require-clean-worktree \
  --artifact-path benchmarks/results/runtime-contract-benchmark-v1-3x.json \
  --report-path docs/metrics/runtime-contract-benchmark-v1-3x.md
```

## Provenance

- Captured at: `2026-08-03T05:37:38.346011Z`
- Commit: `2a4dadb060ef63d60105dfa4128bd30e558959f8`
- Branch: `codex/runtime-contract-benchmark`
- Working tree dirty before execution: `False`
- Manifest snapshot: `sha256:3f23147c15fa752e785e371126d5964ada74658a293f2d1fe970bf847209f35e`
- Evaluation snapshot: `sha256:370583ab4309bd8be53f47f8c83489f320cdd8a65ef0248bca516fd8680a5c52`

## Scope

This is a deterministic runtime-contract regression, not a live-LLM coding benchmark. It supports claims about prompt budgeting, duplicate-read handling, checkpoint validation, and partial-success audit behavior for this frozen source snapshot only. It does not measure general task success, cross-session long-term memory retrieval, model cost, or a real-task read-count reduction rate.
