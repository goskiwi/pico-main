# pico Review Pack

## Project pitch

`pico` is a lightweight local coding agent for repository work. It runs from the terminal, builds context from the current workspace, calls a constrained tool set, and writes local run artifacts for review.

The project demonstrates an end-to-end agent harness rather than a single API wrapper: CLI entrypoints, model adapters, tool validation, approval gates, context management, memory, checkpoints, benchmark fixtures, and run audit output are all present in the repository.

## Architecture map

- `pico/cli.py`: command-line parsing, REPL flow, and OpenAI-compatible client assembly.
- `pico/runtime.py`: agent object, tool guardrails, and runtime capability composition.
- `pico/agent_loop.py`: bounded model/tool loop, stop states, checkpoints, and report lifecycle.
- `pico/actions.py`: normalized `tool`, `final`, and `retry` decisions.
- `pico/tools.py`: explicit tool registry, argument validation, filesystem operations, and shell execution.
- `pico/sandbox.py`: mandatory ephemeral Docker execution with network, privilege, and resource boundaries.
- `pico/context_manager.py`: prompt assembly, context budget decisions, and history shaping.
- `pico/context_history.py`: transcript summarization, task-graph compaction, and history rendering.
- `pico/memory.py`: working memory and durable memory records.
- `pico/run_store.py`: per-run `task_state.json`, `trace.jsonl`, and `report.json` persistence.
- `evaluation/real_benchmark.py`: live-LLM repository benchmark, hidden verification, comparison, and reporting.

## Benchmark evidence

The real-model repository micro-benchmarks live under `benchmarks/`. Hidden verifiers are injected only after the agent stops and run inside the mandatory Docker sandbox. The strongest current evidence is the frozen V3 suite: it was run from a clean committed runtime for three repetitions and passed 13/15 attempts, with four of five tasks passing 3/3. An older V1 before/after run observed 60% versus 90% on the same model and fixture snapshot, but its artifact schema did not lock complete runtime provenance, so it is historical correlation rather than a strict causal ablation. See the [V3 report](../metrics/real-world-benchmark-v3-first-3x.md), [legacy comparison](../metrics/structured-action-comparison.md), and [evaluation methodology](../metrics/evaluation-methodology.md).

The default pytest suite is offline and validates runtime code paths only; Docker integration and live-model tests are explicitly separated, and none is presented as a universal model-capability claim.

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
