# Agent Runtime Architecture

Pico uses `events.jsonl` as the durable source for each Run's process Facts. One `RunProjection` reducer builds TaskState, RunEvidence, RunMetrics, the single Pending Call and final Diff receipt for live execution and replay. Workspace, Project Memory, RepoMap and Artifact stores remain authoritative for their current content.

## Current execution paths

### CLI startup and resume

```text
CLI build_agent
  -> Pico composition root
  -> RuntimeRecovery reads Session.active_run_id
  -> new: RunLifecycle appends user_message + TaskContract before the Session pointer
  -> resume: RunStore validates Run Log v14 and RunProjection replays its Facts
  -> RunLog.reconcile_interrupted resolves an unfinished Tool transaction without replay
  -> run_started / run_resumed
  -> Provider action session reset
  -> AgentLoop
```

### Normal Tool turn

```text
AgentLoop
  -> PromptBuilder / ContextManager
  -> OpenAICompatibleModelClient -> ModelAction.tool
  -> append assistant_tool_call Fact
  -> ToolManager validates Registry / Surface / Schema / Policy / Approval
  -> ToolExecutor captures potential effects and first preimages
  -> append + fsync tool_started Fact
  -> concrete Tool Runner -> ToolRunnerResult
  -> ToolExecutor -> ToolOutcome
  -> append + fsync tool_result Fact
  -> RunProjection applies Fact and RunEvidence derives observations/effects
  -> bounded ToolOutcome returned to the Provider session
```

There is currently no standalone Tool-runtime object. **Tool runtime** names the responsibility
implemented by the real `ToolManager -> ToolExecutor -> Tool Runner` chain.

### Final submission

```text
ModelAction.final
  -> CompletionController assesses TaskContract + Subagents + Evidence + Verifier
  -> blocked: model_instruction + completion_blocked, then continue Provider
  -> allowed: RunLifecycle builds the net final Diff receipt
  -> append assistant_final Fact
  -> RunProjection becomes terminal
  -> clear Session.active_run_id and ExecutionContext

controlled stop
  -> RunLifecycle appends run_stopped + final_diff receipt
  -> workspace drift is reported as unavailable_reason, never as a trustworthy Diff
```

## Terminology

- **Fact**: an accepted durable Run Event in `events.jsonl`. Tool Call, `tool_started`, Tool
  Result, Verification, Compaction and terminal events are Facts. Workspace files, Memory Cards
  and Artifact contents remain facts of their own stores, not duplicated Run Facts.
- **Projection**: the rebuildable in-memory `RunProjection` produced by reducing Facts. It is not
  a second persistence format.
- **Evidence**: `RunEvidence`, derived from Tool Result and Verification Facts. It contains
  observations, side effects, final net changes and verification records.
- **Completion**: the `CompletionController` decision about whether final submission is allowed.
  A Run is terminal only after `RunLifecycle` appends the corresponding terminal Fact.
- **Tool runtime**: the current cross-module execution responsibility described above, not a
  class or persisted object.

## Refactor direction

The durable contracts above are the boundary to preserve. A later refactor may consolidate
Tool-turn orchestration that is currently split across AgentLoop, ToolManager and ToolExecutor,
and may simplify the handoff between CompletionController and RunLifecycle. That consolidation
has not happened yet: current code and diagrams intentionally name only the classes and return
types that exist in this repository.

## State ownership

| State | Durable owner | Projection/cache |
|---|---|---|
| Current Run facts | `runs/<run_id>/events.jsonl` | One RunProjection containing identity, Task, Evidence, Metrics, Pending and final Diff receipt |
| Active Run pointer | Session `active_run_id` | RuntimeRecovery state |
| Task contract | First `user_message.contract` | Goal, task kind, write scope and completion requirements |
| Current task working state | Successful `update_working_state` Tool transactions | Constraints, decisions and next steps prompt section |
| Project memory catalog | Generated `MEMORY.md` | Bounded resident section |
| Recalled project knowledge | Explicit `memory_recall` Tool Call/Result | Complete Markdown Card bodies under a bounded recall budget |
| Large redacted output | Artifact files | Run Log artifact reference |
| Child receipts | Child Run Logs and Patch files | In-process Subagent DAG state |

`task_state.json`, `context.jsonl` and Checkpoint snapshots do not exist.

## Run events

Every event has one strict schema, contiguous sequence and stable ID:

```text
user_message
run_started / run_resumed
model_requested / turn_metrics
assistant_tool_call
tool_started
tool_result
model_instruction
provider_session_reset
verification_result
compaction
completion_blocked
assistant_final / run_stopped
```

The Run Log is single-writer and fsynced after every accepted event. It has no hash chain: a hash stored beside mutable local data is not a trusted tamper boundary. Contiguous sequence and IDs plus strict User, Tool Call/Started/Result, and terminal payloads reject causal corruption; telemetry payloads remain extensible. A final incomplete JSONL tail is a crash artifact and is truncated; malformed complete events fail closed.

Replaceable snapshots and Workspace mutations use same-directory temporary files plus atomic replace. `write_file` only creates an absent file; `edit_file` only changes an existing file and stages/fsyncs the complete payload before revalidating its expected revision at the `os.replace` commit point. A mismatch preserves external content and returns structured expected/actual revision facts for a model-driven re-read and repair.

## Tool recovery

Before execution, `tool_started` records:

- stable call ID and canonical arguments from `assistant_tool_call`;
- effect scope;
- exact potential paths;
- each path's before state/revision.

After execution, `tool_result` records the canonical ToolOutcome. On resume:

- call without `tool_started`: append a not-started error;
- started call with unchanged declared paths: append interrupted error;
- changed declared paths: append partial result;
- mutating tool without enumerable paths: append unknown result;
- never replay an unfinished non-idempotent tool automatically.

ToolOutcome keeps three explicit state dimensions for direct inspection:

- `status`: overall result (`success`, `error`, `rejected`, or `partial_success`);
- `execution_state`: whether the Tool Runner was not started, returned normally, or failed/interrupted;
- `side_effect_state`: whether effects are absent, changed, partial, or unknown.

Tool Runners return machine-readable facts plus `FailureInfo` through `ToolRunnerResult`. The durable failure stores one recovery condition (`retry_after_change`, `retry_after_wait`, `user_action_required`, or `no_retry`); Runtime derives the model-facing correction action instead of persisting a second decision. Repeat admission only blocks a matching call after `partial/unknown` side effects. Manual mode only permits observation tools; mutations require an active Run and exact persisted Pending Call.

## Context and compaction

Stable Runtime policy is sent through Responses `instructions`; dynamic Workspace, TaskContract, WorkingState, Memory, RepoMap, History and current request enter `input`; Function Schemas remain in `tools`. Prompt construction is read-only. Before a fresh build, AgentLoop may explicitly prepare Compaction: an isolated Provider session summarizes only historical Progress and Critical Context, while TaskContract and WorkingState remain canonical. Invalid, failed or non-shrinking summaries commit no event and use a bounded suffix of complete Tool Call/Result transactions. Original events remain durable.

## Resume

Session stores only `active_run_id`. On startup Pico opens that Run Log, repairs an incomplete final line, reduces events through the same RunProjection used live, reconciles an unfinished Tool, rebuilds Context, resets the Provider session and continues with current Runtime configuration. Resume must keep the persisted TaskContract requirements. A terminal Run Log clears `active_run_id`.

## Completion

Completion first enforces the persisted TaskContract: read-only tasks need a successful Observation; modify tasks that require change need a non-empty final RunChangeSet; tasks requiring verification need an explicit command and a passed result for the current mutation/path state. Each path stores one initial preimage, so `A -> B -> A` is touched but not a net change; successful settlement persists the actual final Unified Diff Artifact. External drift blocks successful completion. A user cancellation or reset can still terminalize safely, but its receipt explicitly records `unavailable_reason=workspace_drift` instead of claiming a trustworthy Diff. Verification freshness is derived from its mutation sequence and path states rather than stored as a mutable label. Unresolved partial/unknown effects and unapplied Child patches remain blockers; there is no implicit language-specific AST gate.

### Bounded workspace freshness design

Pico previously opened every non-ignored workspace file, computed a SHA-256 for each, and hashed the complete map before and after verification. That cost scales with total repository bytes and duplicated facts already owned by Git and the Run Log, so the full-workspace fingerprint path and API were removed. The current design reuses the existing per-path content state only for paths already established as changed by RunEvidence.

A Git worktree version remains a possible stronger contract if Pico later must detect IDE or other process changes anywhere in the repository at Completion. It would combine HEAD, staged index identity, porcelain-v2 status, dirty/untracked content revisions, explicit Runtime-affected ignored paths, and submodule state. It is not implemented now: it adds Git scans, non-Git fallback semantics, ignored-file and submodule edge cases beyond the concrete changed-path freshness contract. Stale writes remain protected separately by expected-revision checks at the file commit point.
