# pico Review Pack

## 10–15 minute review route

1. Read the project pitch and architecture map below.
2. Inspect `pico/context_manager.py` → `pico/repo_map.py` → `pico/run_store.py`.
3. Follow `pico/models.py` → `pico/agent_loop.py` → `pico/tools.py` → `pico/sandbox.py`.
4. Inspect `pico/run_store.py` and `pico/run_undo.py`, then the
   [Repo Map + Undo result](../metrics/reliability-benchmark-v1-live-3x.md).
5. Read the [V5 budget decision](../metrics/real-world-benchmark-v5-repo-map-budget-600-vs-full-first-3x.md),
   then finish with the [security model](../security-model.md) and
   [metrics evidence map](../metrics/README.md).

## Project pitch

`pico` is a local coding-agent runtime focused on task-aware context selection,
continuous tool conversations, bounded execution, recoverable workspace changes, and
auditable evidence. It is not a wrapper around one LLM call: the repository contains
tokenizer-aware context budgeting, a Repo Map, session working memory, the
model/tool state machine, Pydantic tool contracts, Docker-only shell boundary, run
artifacts, an Undo journal, and a hidden-verifier benchmark runner.

## Architecture map

- `pico/repo_map.py`: tree-sitter symbols, weighted relations, Personalized PageRank,
  incremental refresh, and budgeted rendering.
- `pico/context_manager.py`: real-token budgets for Repo Map, session state, on-demand skills, and compacted task evidence.
- `pico/skills.py`: Agent Skills-compatible metadata validation, index-only discovery, and explicit manual-only skills.
- `pico/context_compaction.py`: structured checkpoint compiler with bounded recent original evidence.
- `pico/models.py`: Responses functions and native function-output replay.
- `pico/agent_loop.py`: bounded model/tool/final transitions plus threshold/overflow task compaction.
- `pico/tools.py`: one Pydantic schema source, capability checks, and tool execution.
- `pico/sandbox.py`: mandatory network-disabled Docker execution.
- `pico/run_undo.py`: first-touch preimages, conflict preflight, and restoration.
- `pico/run_store.py`: task state, trace, foldable task canvas, reports, and full tool outputs.
- `evaluation/real_benchmark.py`: frozen fixtures, hidden verifiers, isolation audit,
  and evidence collection.

The detailed flow is in the
[agent harness overview](../architecture/agent-harness-v1-overview.md).

## Benchmark evidence

V5 is the active frozen regression suite: dynamic and 600-token maps both passed
14/15, but the measured cost reduction missed the pre-registered threshold, so the
dynamic default was not changed. The earlier V4 A/B result (`full` 13/15 versus
`no_repo_map` 6/15) remains historical evidence for the Repo Map design; it is not
duplicated as a second fixture-contract test suite.

After the audit-state simplification, a clean-worktree V5 `full` rerun completed
three times at 5/5 each (15/15 total), with no model failures or Action rejections;
see the [post-simplification report](../../artifacts/live-llm-v5-post-simplification-3x.md).

The reliability suite joins retrieval with recovery: Repo Map localization passed 3/3,
both Undo scenarios recovered 6/6, and complete post-Undo workspace digests matched
their pre-run digests 6/6.

These are small, scenario-specific engineering regressions. They are not universal
model-capability claims.

## Test focus

For an interview, read the invariant tests rather than every fixture self-check:

- `tests/test_agent_loop.py`: live tool-result continuity, task compaction, bounded completion;
- `tests/test_safety_invariants.py` and `tests/test_run_undo.py`: local policy and all-or-nothing recovery;
- `tests/test_context_tokens.py` and `tests/test_repo_map.py`: bounded context selection;
- `tests/test_reliability_benchmark.py` and `tests/test_real_benchmark_v5.py`: frozen end-to-end evidence.

Useful checks:

```bash
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q pico tests scripts
```

## Sample run artifact list

Each run writes under `.pico/runs/<run_id>/`:

- `task_state.json`: state machine snapshot for one `ask()`;
- `trace.jsonl`: ordered prompt, model, tool, checkpoint, Undo, and finish events;
- `task.mmd`: active Mermaid task canvas with node-level evidence references;
- `phases/phase_XXX.mmd`: archived task stages that can be opened from the active canvas;
- `offload.jsonl`: tool-level summaries, node IDs, and reference paths;
- `context_compactions.jsonl`: structured checkpoints, triggers, workspace fingerprints, and model metadata;
- `report.json`: final status, prompt metadata, summary, audit, and rejected Actions;
- `undo/manifest.json` plus blobs: restorable preimages and expected post-run states;
- `refs/*.txt`: complete evidence kept out of normal prompt context.

For a Chinese interview walkthrough, use [`interview-notes.md`](interview-notes.md).
