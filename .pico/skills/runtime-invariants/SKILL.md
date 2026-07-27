---
name: runtime-invariants
description: Change Pico's agent loop, context assembly, memory, tool conversation, or task-canvas runtime without breaking its audited execution invariants. 中文: Agent 运行时, 上下文, 记忆, 工具调用, 任务画布.
when_to_use: Use for behavior changes in pico/agent_loop.py, context_manager.py, models.py, tools.py, tool_runtime.py, run_store.py, or their tests.
when_not_to_use: Ordinary application-code changes, a read-only PR review, or generic debugging outside Pico runtime control flow.
tools: [list_files, read_file, search, query_repo_map, read_task_canvas, read_task_event, read_tool_output, write_file, patch_file, run_shell]
allowed_tools_strict: true
priority: 90
conflicts_with: [code-review, security-and-undo-review, run-artifact-audit]
---

# Runtime invariants

Use this workflow before changing Pico's core runtime.

1. Locate the control-flow owner and its focused tests before editing.
2. Preserve exact provider-side function-call outputs between tool turns. A task
   canvas, summary, or memory note must never replace the matching raw tool
   result in the live conversation.
3. Keep prompt budgets measured by the configured tokenizer. Do not introduce
   character-count token estimates.
4. Treat the task canvas as an audit and recovery control plane. Folded phases
   must remain addressable through their phase artifacts.
5. Recover a provider conversation only after an explicit context-limit error;
   the recovery bundle must stay bounded and preserve the newest useful
   evidence.
6. Keep local validation, approval, sandbox, and Run Undo as independent
   enforcement layers. A prompt instruction is never a substitute for them.
7. Add or update the smallest behavior-level regression test, then run the
   focused test before the relevant broader suite.

In the final answer, state the invariant preserved, the files changed, and the
verification evidence.
