# Pico Review Pack

Pico is a compact, single-protocol local multi-agent coding Runtime.

- `pico.contracts`: provider-neutral Runtime contracts after native Responses parsing.
- `pico.run_log` / `pico.context_manager`: single-source causal state, compaction and Token governance.
- `pico.repo_map`: tree-sitter symbol graph and task-ranked projection.
- `pico.features.memory` / `pico.project_memory`: RunLog-projected goal, constraints, decisions and next steps plus Markdown long-term facts recalled through explicit, audited Tool transactions.
- `pico.tool_executor` / `pico.mutations` / `pico.sandbox`: staged admission, atomic edits and Docker execution.
- `pico.runtime_recovery` / `pico.run_store`: Run Log-tail resume, operation receipts and artifacts.
- `pico.evidence` / `pico.verification` / `pico.completion_controller`: evidence-bound completion.
- `pico.subagents`: bounded DAG scheduling, isolated Child Runtime state, Git Worktrees and a separate receipt-bound Patch integrator.
- `pico.evaluation`: deterministic Runtime regressions and mechanism evaluations.
- `pico.runtime_*`: explicit Config, Workspace, Session, Run, Tool, Recovery and Prompt ownership;
  `pico.runtime.Pico` is the small composition root.
- `pico.agent_loop` / `pico.run_lifecycle` / `pico.completion_controller`: model turns,
  durable Run lifecycle and completion authority have separate owners.

## 30-second interview framing

Pico is not an LLM chat wrapper. It is the control plane around a coding model: it selects bounded repository context, admits and isolates tool calls, records side effects as evidence, resumes interrupted work without blindly replaying mutations, and blocks completion until Runtime-owned checks agree with the current workspace.

The three core deep dives are:

1. Continuous Responses tool context, Run Log reset/resume, and RepoMap.
2. Revision-bound atomic mutations plus interrupted-operation reconciliation.
3. Single-source Run Log, workspace-bound verification, and the Completion Gate.

Multi-Agent DAG scheduling and isolated Patch integration are an optional fourth deep dive,
after the single-Agent Runtime is understood.

See [Agent Runtime](../architecture/agent-runtime.md) for invariants, [resume wording](../resume-project.md) for claims aligned with the code, and [the interview demo](interview-demo.md) for a short evidence-first walkthrough.
