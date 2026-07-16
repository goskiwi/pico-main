# pico Review Pack

## Project pitch

`pico` is a lightweight local coding agent for repository work. It runs from the terminal, builds context from the current workspace, calls a constrained tool set, and writes local run artifacts for review.

The project demonstrates an end-to-end agent harness rather than a single API wrapper: CLI entrypoints, model adapters, tool validation, approval gates, context management, memory, checkpoints, benchmark fixtures, and run audit output are all present in the repository.

## Architecture map

- `pico/cli.py`: command-line parsing, REPL flow, and provider selection.
- `pico/runtime.py`: agent object, tool guardrails, and runtime capability composition.
- `pico/agent_loop.py`: bounded model/tool loop, stop states, checkpoints, and report lifecycle.
- `pico/actions.py`: provider-independent `tool`, `final`, and `retry` decisions.
- `pico/tools.py`: explicit tool registry, argument validation, filesystem operations, and shell execution.
- `pico/sandbox.py`: mandatory ephemeral Docker execution with network, privilege, and resource boundaries.
- `pico/context_manager.py`: prompt assembly, context budget decisions, and history shaping.
- `pico/context_history.py`: transcript summarization, task-graph compaction, and history rendering.
- `pico/memory.py`: working memory and durable memory records.
- `pico/run_store.py`: per-run `task_state.json`, `trace.jsonl`, and `report.json` persistence.
- `evaluation/real_benchmark.py`: live-LLM repository benchmark, hidden verification, comparison, and reporting.

## Benchmark evidence

The real-model V1 benchmark lives in `benchmarks/real_world_tasks.json`; the independent held-out set lives in `benchmarks/real_world_tasks_v2.json`. Hidden verifiers are injected only after the agent stops and run inside the mandatory Docker sandbox. On the same V1 model and fixture snapshot, structured Actions improved pass rate from 60% to 90% and reduced average model calls from 9.5 to 6.4. The five-task V2 held-out run passed 5/5 with zero rejected Actions. See the [matched comparison](../metrics/structured-action-comparison.md), [structured V1 report](../metrics/real-world-benchmark-v1-structured.md), and [V2 held-out report](../metrics/real-world-benchmark-v2-heldout.md).

The pytest suite is offline and validates runtime code paths only; it is not presented as evidence of model capability.

The design follows the [Responses function-calling loop](https://developers.openai.com/api/docs/guides/function-calling): strict functions represent runtime actions, `submit_final` is explicit, and each function result is returned using its `call_id`. The local client keeps the structured conversation items itself, so it also works with compatible endpoints that do not retain `previous_response_id` state.

Useful checks:

```bash
uv run pytest -q
uv run ruff check .
```

## Sample run artifact list

Each run writes artifacts under `.pico/runs/<run_id>/`:

- `task_state.json`: compact state machine snapshot for the current `ask()` call.
- `trace.jsonl`: ordered event stream with prompt, model, tool, checkpoint, and finish events.
- `report.json`: final report with status, stop reason, prompt metadata, run summary, tool audit entries, and rejected model Actions.

For a Chinese interview walkthrough, use [`interview-notes.md`](interview-notes.md).
