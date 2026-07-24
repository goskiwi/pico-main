# Reliability benchmark V1 protocol

> Frozen before the first live-model execution. Results must be published even
> when an acceptance criterion fails; prompts, fixtures, verifiers, and gates
> must not be adjusted after inspecting live outcomes.

## Question

This benchmark asks whether the current runtime can demonstrate three parts of
one reliability story under real model control:

1. complete a cross-module task in a repository with inactive
   legacy/experimental distractors using the default Repo Map;
2. detect a deliberately rejected two-file agent change and restore the exact
   pre-run workspace with Run Undo;
3. restore a file to its pre-run dirty bytes after the agent modifies that same
   file, rather than restoring the pristine fixture.

It is an engineering regression, not a general coding-capability benchmark and
not a new Repo Map ablation. The existing V4 ablation remains the evidence for
the observed difference between `full` and `no_repo_map`.

## Frozen execution

- Benchmark manifest: `benchmarks/reliability_tasks_v1.json`
- Model: repository `OPENAI_MODEL` (expected `gpt-5.4`)
- Provider: OpenAI-compatible Responses API
- Temperature: 0
- Repetitions: 3 per scenario, 9 attempts total
- Runtime: clean Git worktree, default dynamic Repo Map, conflict-safe Run Undo
- Verification: network-disabled Docker sandbox with hidden verifier files
- Workspace: a fresh fixture copy for every attempt

Run:

```bash
uv run python scripts/run_reliability_benchmark.py \
  --repetitions 3 \
  --require-clean-worktree \
  --workspace-root /tmp/pico-reliability-v1-workspaces \
  --artifact-path artifacts/reliability-benchmark-v1-live-3x.json \
  --report-path docs/metrics/reliability-benchmark-v1-live-3x.md
```

The runner reads only `OPENAI_API_BASE`, `OPENAI_API_KEY`, and `OPENAI_MODEL`
from the repository `.env.local`.

## Acceptance criteria

Every attempt fails closed on a model/runtime failure, trace parse error,
workspace-isolation violation, missing expected source mutation, or verifier
mismatch.

### `repo_map_cross_module`

- Agent run reaches `completed`.
- `ops_center/inventory/policy.py` changes.
- Hidden verifier exits 0.
- All 3 repetitions pass.

### `undo_rejected_multifile_change`

- Agent changes both `checkout/pricing.py` and `checkout/service.py`.
- Hidden verifier rejects the post-agent workspace.
- Undo dry-run and apply both include the expected paths.
- The complete post-Undo workspace digest equals the pre-run digest.
- Hidden verifier exits 0 after Undo.
- All 3 repetitions pass.

### `undo_preserves_preexisting_dirty_file`

- Before the agent starts, the runner appends a frozen owner note to
  `README.md`.
- Agent changes both that README and `checkout/pricing.py`.
- Hidden verifier rejects the post-agent workspace.
- Undo restores the complete pre-run workspace digest.
- Restored `README.md` equals its pre-run dirty SHA-256 and differs from the
  pristine fixture SHA-256.
- Hidden verifier exits 0 after Undo.
- All 3 repetitions pass.

## Recorded evidence

The JSON artifact records per-attempt model calls, tokens, latency, tool steps,
Repo Map selected files, actual mutations, verifier outputs, Undo dry-run and
restored paths, full-workspace digests, and four-stage SHA-256 evidence for
relevant paths:

1. pristine fixture;
2. pre-run workspace;
3. post-agent workspace;
4. post-Undo workspace.

The generated Markdown report aggregates task success, Undo recovery, exact
restoration, dirty-file preservation, cost, and latency. Remote model labels
can change behind an API, and three repetitions are only a small stability
sample; conclusions must retain those limits.
