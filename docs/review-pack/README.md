# pico Review Pack

## 10–15 minute review route

1. Read the project pitch and architecture map below.
2. Inspect `pico/repo_map.py`, then read the
   [Repo Map A/B report](../metrics/real-world-benchmark-v4-repo-map-ablation-3x.md).
3. Follow `pico/agent_loop.py` → `pico/tools.py` → `pico/sandbox.py`.
4. Inspect `pico/run_undo.py` and the
   [Repo Map + Undo result](../metrics/reliability-benchmark-v1-live-3x.md).
5. Finish with the [security model](../security-model.md) and
   [metrics evidence map](../metrics/README.md).

## Project pitch

`pico` is a local coding-agent runtime focused on task-aware repository retrieval,
bounded execution, recoverable workspace changes, and auditable evidence. It is not
a wrapper around one LLM call: the repository contains the model/tool state machine,
Pydantic tool contracts, Docker-only shell boundary, context budgeting, run artifacts,
Undo journal, and hidden-verifier benchmark runner.

## Architecture map

- `pico/repo_map.py`: tree-sitter symbols, weighted relations, Personalized PageRank,
  incremental refresh, and budgeted rendering.
- `pico/context_manager.py`: prompt sections and token budgets.
- `pico/models.py`: strict Responses function calls normalized to `ModelAction`.
- `pico/agent_loop.py`: bounded model/tool/final transitions.
- `pico/tools.py`: one Pydantic schema source, capability checks, and tool execution.
- `pico/sandbox.py`: mandatory network-disabled Docker execution.
- `pico/run_undo.py`: first-touch preimages, conflict preflight, and restoration.
- `pico/run_store.py`: task state, trace, task graph, reports, and full tool outputs.
- `evaluation/real_benchmark.py`: frozen fixtures, hidden verifiers, isolation audit,
  and evidence collection.

The detailed flow is in the
[agent harness overview](../architecture/agent-harness-v1-overview.md).

## Benchmark evidence

The strongest focused Repo Map evidence is V4: five cross-module tasks with inactive
look-alike implementations, run three times per variant from a clean commit.
`full` passed 13/15 and `no_repo_map` passed 6/15. V5 then tested the concrete budget
decision: dynamic and 600-token maps both passed 14/15, but the measured cost reduction
missed the pre-registered threshold, so the default was not changed.

The reliability suite joins retrieval with recovery: Repo Map localization passed 3/3,
both Undo scenarios recovered 6/6, and complete post-Undo workspace digests matched
their pre-run digests 6/6.

These are small, scenario-specific engineering regressions. They are not universal
model-capability claims.

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
- `task_graph.mmd`: compact execution graph with tool-output references;
- `report.json`: final status, prompt metadata, summary, audit, and rejected Actions;
- `undo/manifest.json` plus blobs: restorable preimages and expected post-run states;
- `tool_outputs/*.txt`: complete outputs kept out of normal prompt history.

For a Chinese interview walkthrough, use [`interview-notes.md`](interview-notes.md).
