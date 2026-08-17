# Pico Review Pack

Pico is a compact, single-protocol local coding-agent Runtime.

- `pico.contracts`: provider-neutral Runtime contracts after native Responses parsing.
- `pico.context_ledger` / `pico.context_manager`: causal state, transactional compaction and Token governance.
- `pico.repo_map`: tree-sitter symbol graph and task-ranked projection.
- `pico.features.memory` / `pico.project_memory`: revision-bound working state and Markdown long-term facts.
- `pico.tool_executor` / `pico.mutations` / `pico.sandbox`: staged admission, atomic edits and Docker execution.
- `pico.checkpoint` / `pico.run_store`: strict resume, operation receipts and run artifacts.
- `pico.evidence` / `pico.verification` / `pico.completion`: evidence-bound completion.
- `pico.evaluation`: deterministic Runtime regressions and mechanism evaluations.

## 30-second interview framing

Pico is not an LLM chat wrapper. It is the control plane around a coding model: it selects bounded repository context, admits and isolates tool calls, records side effects as evidence, resumes interrupted work without blindly replaying mutations, and blocks completion until Runtime-owned checks agree with the current workspace.

The three strongest deep dives are:

1. Continuous Responses tool context, Context Ledger reset/resume, and RepoMap.
2. Revision-bound atomic mutations plus interrupted-operation reconciliation.
3. Hash-chained events, workspace-bound verification, and the Completion Gate.

See [Agent Runtime](../architecture/agent-runtime.md) for invariants, [resume wording](../resume-project.md) for claims aligned with the code, and [the interview demo](interview-demo.md) for a short evidence-first walkthrough.
