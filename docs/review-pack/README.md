# Pico Review Pack

Pico is a compact, single-protocol local multi-agent coding Runtime.

- `pico.contracts`: provider-neutral Runtime contracts after native Responses parsing.
- `pico.run_log` / `pico.context_manager`: single-source causal state, minimal conditional dynamic input, two-section semantic compaction and Token governance.
- `pico.repo_map`: tree-sitter symbol graph and task-ranked projection.
- `pico.features.memory` / `pico.project_memory`: RunLog-projected WorkingState constraints, decisions and next steps plus cross-Run Markdown cards recalled through explicit, audited Tool transactions. Goal belongs to TaskContract.
- `pico.tool_runtime` / `pico.mutations` / `pico.sandbox`: staged admission, atomic edits and Docker execution.
- `pico.run_lifecycle` / `pico.run_store`: Run Log-tail resume, operation receipts and artifacts without a second recovery-state object.
- `pico.evidence` / `pico.verification` / `pico.completion_controller`: evidence-bound completion.
- `pico.subagents`: bounded DAG scheduling, isolated Child Runtime state, Git Worktrees and a separate receipt-bound Patch integrator.
- `evals`: repository-local deterministic Runtime regressions and mechanism evaluations; it is excluded from the installable `pico` package.
- `pico.runtime_*`: explicit Config, Workspace, Session, Run, Tool and Prompt ownership;
  `pico.runtime.Pico` is the small composition root.
- `pico.agent_loop` / `pico.run_lifecycle` / `pico.completion_controller`: model turns,
  durable Run lifecycle and completion authority have separate owners.

Prompt/Provider review uses three explicit Tool surfaces: complete native `declared_tools`, dynamic
`allowed_tool_names`, and Token-accounted `wire_tools`. Verified backends can keep complete schemas
stable and restrict `tool_choice.allowed_tools` during ordinary execution. The final-only boundary
physically narrows the wire schema to `submit_final` and rebuilds the Provider session; cache evidence
comes from Provider usage fields.

The review demo's seven-category Effective Recovery Context is a teaching/observability composition,
not a seven-section summary. Semantic Compaction generates and persists only Progress and Critical
Context. Goal comes from TaskContract, constraints/decisions/next steps from WorkingState, and
execution evidence from RunEvidence; the composed view is neither durable state nor completion input.

## 30-second interview framing

Pico is not an LLM chat wrapper. It is the control plane around a coding model: it selects bounded repository context, admits and isolates tool calls, records side effects as evidence, resumes interrupted work without blindly replaying mutations, and blocks completion until Runtime-owned checks agree with the current workspace.

The three core deep dives are:

1. Continuous Responses tool context, Run Log reset/resume, and RepoMap.
2. Revision-bound atomic mutations plus interrupted-operation reconciliation.
3. Single-source Run Log, workspace-bound verification, and the Completion Gate.

Multi-Agent DAG scheduling and isolated Patch integration are an optional fourth deep dive,
after the single-Agent Runtime is understood.

See [Agent Runtime](../architecture/agent-runtime.md) for invariants, [resume wording](../resume-project.md) for claims aligned with the code, and [the interview demo](interview-demo.md) for a short evidence-first walkthrough.
