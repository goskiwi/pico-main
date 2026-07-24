# Pico reliability benchmark V1

## Result

- Overall: **9/9 (100.0%)** attempts satisfied their pre-registered acceptance criteria.
- Undo recovery: **6/6 (100.0%)**.
- Exact whole-workspace restoration: **100.0%**.
- Pre-existing dirty-file preservation: **100.0%**.
- Recorded model failures / Action rejections / trace parse errors / workspace-isolation failures: **0 / 0 / 0 / 0**.

## Per-scenario metrics

| Scenario | Mode | Passed | Avg tool steps | Avg model calls | Tokens in/out | Avg duration |
|---|---|---:|---:|---:|---:|---:|
| `repo_map_cross_module` | task_success | 3/3 | 9.00 | 10.00 | 104471/3965 | 169.07s |
| `undo_preserves_preexisting_dirty_file` | undo_recovery | 3/3 | 4.00 | 5.00 | 28457/881 | 59.67s |
| `undo_rejected_multifile_change` | undo_recovery | 3/3 | 4.00 | 5.00 | 28159/920 | 81.29s |

## Attempt evidence

| Scenario | Rep | Pass | Mutations | Pre-undo verifier | Restored | Dirty preserved | Repo Map files |
|---|---:|---:|---:|---:|---:|---:|---:|
| `repo_map_cross_module` | 1 | yes | 2 | 0 | n/a | n/a | 24 |
| `undo_rejected_multifile_change` | 1 | yes | 2 | 1 | yes | n/a | 4 |
| `undo_preserves_preexisting_dirty_file` | 1 | yes | 2 | 1 | yes | yes | 4 |
| `repo_map_cross_module` | 2 | yes | 2 | 0 | n/a | n/a | 24 |
| `undo_rejected_multifile_change` | 2 | yes | 2 | 1 | yes | n/a | 4 |
| `undo_preserves_preexisting_dirty_file` | 2 | yes | 2 | 1 | yes | yes | 4 |
| `repo_map_cross_module` | 3 | yes | 1 | 0 | n/a | n/a | 24 |
| `undo_rejected_multifile_change` | 3 | yes | 2 | 1 | yes | n/a | 4 |
| `undo_preserves_preexisting_dirty_file` | 3 | yes | 2 | 1 | yes | yes | 4 |

## Protocol

- Model: `gpt-5.4`; temperature 0; 3 repetitions per scenario.
- `repo_map_cross_module`: a hidden verifier accepts a cross-module change in a repository containing legacy/experimental distractors.
- `undo_rejected_multifile_change`: the model is asked to make a two-file change that a hidden baseline verifier rejects; Undo must restore the exact pre-run workspace.
- `undo_preserves_preexisting_dirty_file`: the runner creates a pre-run README edit, asks the model to modify that same file and source code, then requires Undo to restore the dirty pre-run bytes rather than the pristine fixture.
- Workspace digests compare every non-runtime file; path evidence records pristine, pre-run, post-agent, and post-Undo SHA-256 values.

## Provenance

- Captured at: `2026-07-24T06:09:52.375738Z`
- Commit: `5d80ce56f63c3f9aa4649725fab71b3271d92666`
- Branch: `codex/reliability-evaluation`
- Working tree dirty before execution: `False`
- Fixture snapshot: `sha256:d6f56102bb9e298b94b6341b05025524ce8a25cabed95493e3719979f71ca901`
- Evaluation snapshot: `sha256:67ea15e520955191a4dace0262c550febb732766f381a4db540360cb9532b5e3`

## Scope

This is a nine-attempt engineering regression over three small scenarios, not a general coding-capability benchmark. It demonstrates observed task completion and restoration behavior for this frozen snapshot and model configuration.
Provider transport retries that eventually succeed inside the model SDK are not emitted as model failures in Pico's trace, so the artifact does not quantify transient HTTP retry frequency.
