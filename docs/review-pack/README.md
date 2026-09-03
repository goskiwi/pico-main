# Pico Review Pack

Pico is a compact, single-protocol local multi-agent coding Runtime.

- `pico.contracts`: provider-neutral Runtime contracts after native Responses parsing.
- `pico.run_log` / `pico.prompt_builder`: single-source causal state, minimal conditional dynamic input, two-section semantic compaction and Token governance.
- `pico.repo_map`: tree-sitter symbol graph and task-ranked projection.
- `pico.working_state`: RunLog-projected constraints, decisions and next steps. Goal belongs to TaskContract.
- `pico.tool_runtime` / `pico.mutations`: staged admission, revision-bound atomic edits, and one approval-bound diagnostic command.
- `pico.run_lifecycle` / `pico.run_store`: Run Log-tail resume, operation receipts and artifacts without a second recovery-state object.
- `pico.command_runner` / `pico.verification`: trusted host execution for the Runtime-configured Verification command, not a sandbox.
- `pico.evidence` / `pico.completion_controller`: evidence-bound completion.
- `pico.subagents`: one synchronous Child delegation at a time, read-only explore, Worktree-isolated implement, and explicit receipt-bound integration.
- `applications.coding`: opt-in post-Verification Git commit restricted to clean Run-changed paths.
- `pico.runtime_*`: explicit Config, Workspace, Session, Run, Tool and Prompt ownership;
  `pico.runtime.Pico` is the small composition root.
- `pico.agent_loop` / `pico.run_lifecycle` / `pico.completion_controller`: model turns,
  durable Run lifecycle and completion authority have separate owners.

Prompt/Provider review uses one current Tool surface derived from TaskContract and the remaining
Tool budget. That exact schema set is sent to the Provider and charged to the Token budget; the
final-only boundary narrows it to `submit_final` and rebuilds the Provider session.

The review demo's seven-category Effective Recovery Context is a teaching/observability composition,
not a seven-section summary. Semantic Compaction generates and persists only Progress and Critical
Context. Goal comes from TaskContract, constraints/decisions/next steps from WorkingState, and
execution evidence from RunEvidence; the composed view is neither durable state nor completion input.

## 30-second interview framing

Pico is not an LLM chat wrapper. It is the control plane around a coding model: the user selects Ask, Code or Auto; Runtime turns that explicit policy into a narrow Tool surface, admits and audits calls, records side effects as evidence, resumes interrupted work without blindly replaying mutations, and blocks completion until Runtime-owned checks agree with the current workspace.

The Runtime is for trusted local repositories. A user-configured fixed Verification command runs
locally with the current user's permissions; it is not protected by the structured file-Tool path
boundary. Unknown code requires an external CI runner, VM, or container.

The three core deep dives are:

1. Continuous Responses tool context, Run Log reset/resume, and RepoMap.
2. Revision-bound atomic mutations plus interrupted-operation reconciliation.
3. Single-source Run Log, workspace-bound verification, and the Completion Gate.

Single-Child delegation and explicit Patch integration are an optional fourth deep dive,
after the single-Agent Runtime is understood.

See [Agent Runtime](../architecture/agent-runtime.md) for invariants, [resume wording](../resume-project.md) for claims aligned with the code, and [the interview demo](interview-demo.md) for a short evidence-first walkthrough.
