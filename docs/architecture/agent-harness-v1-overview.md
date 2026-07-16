# Agent Harness v1 Overview

`pico` is organized around a small agent harness that turns one user request into a bounded sequence of model calls and tool executions.

```text
user request
  -> CLI builds Pico runtime
  -> task state and run directory are created
  -> ContextManager builds the prompt
  -> model client returns a normalized ModelAction
  -> native function_call / function_call_output advances the action loop
  -> tool validation, approval, and execution run
  -> session, trace, task state, and report are persisted
```

The runtime keeps the task state separate from the session. The session stores recoverable conversation and memory, while task state records the status of a single `ask()` run: attempts, tool steps, last tool, stop reason, checkpoint id, and final answer.

The tool layer is intentionally explicit. The model can only request registered tools, every tool has argument validation, and risky tools pass through approval and workspace-diff accounting. Shell commands also pass a dangerous-command screen before execution.

For OpenAI-compatible Responses backends, every workspace tool is exposed as a strict function and `submit_final` is a separate strict function. The client keeps the function call and its output as structured conversation items, so the runtime does not have to recover JSON/XML from prose. Text-only backends normalize their response inside the provider adapter; the main loop sees only `tool`, `final`, or `retry`. Rejected actions are recorded in both trace and report artifacts.

Run observability is split into two artifacts:

- `trace.jsonl` is the detailed timeline for debugging a run.
- `report.json` is the final summary for review, metrics, and benchmark evidence.
