# Agent Runtime Architecture

Pico uses `events.jsonl` as the durable source for each Run's process facts. WorkingState, TaskState, RunEvidence, Context, and run statistics are projections; Workspace, Project Memory, and Artifact stores remain authoritative for their current content.

```text
User request
  -> Run Log user_message
  -> bounded Context projection + RepoMap + Memory
  -> Responses ModelAction
  -> admission / approval
  -> assistant_tool_call
  -> fsynced tool_started with potential effects
  -> ToolRunnerResult / ToolOutcome
  -> fsynced tool_result
  -> TaskState + Evidence projection
  -> verification_result
  -> assistant_final or run_stopped
```

## State ownership

| State | Durable owner | Projection/cache |
|---|---|---|
| Current Run facts | `runs/<run_id>/events.jsonl` | WorkingState, TaskState, Evidence, Context, run statistics |
| Active Run pointer | Session `active_run_id` | RuntimeRecovery state |
| Current task working state | `user_message` plus successful `update_working_state` Tool transactions | Goal, constraints, decisions, and next steps prompt section |
| Project memory catalog | Generated `MEMORY.md` | Bounded resident section |
| Recalled project knowledge | Explicit `memory_recall` Tool Call/Result | Complete Markdown Card bodies under a bounded recall budget |
| Large redacted output | Artifact files | Run Log artifact reference |
| Child receipts | Child Run Logs and Patch files | In-process Subagent DAG state |

`task_state.json`, `context.jsonl` and Checkpoint snapshots do not exist.

## Run events

Every event has one strict schema, contiguous sequence and stable ID:

```text
run_started / run_resumed
user_message
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

Replaceable snapshots and Workspace mutations use temporary files plus atomic replace. Pico targets ordinary process-crash recovery and does not claim a cross-filesystem or power-loss transaction across every content store.

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
- `side_effect_state`: whether effects are absent, changed, partial, or unknown;
Tool Runners return structured `FailureInfo` through `ToolRunnerResult`; Runtime classification never parses display text to recover exit codes or failure state.

## Context and compaction

The model Context uses the configured provider window and includes every active context-bearing Run event. Compaction starts when usage crosses the window minus its output reserve and keeps a recent token budget without splitting Tool Call/Result transactions. OpenAI-compatible clients use an isolated model session to replace the exact older prefix with a strict six-section semantic brief: Goal, Constraints & Preferences, Progress, Key Decisions, Next Steps and Critical Context. The brief is derived context; WorkingState and Tool results remain authoritative. Invalid, failed or non-shrinking semantic summaries fall back to the deterministic Tool-transaction summary. A reported context overflow gets at most one compact-and-retry attempt. Original events remain in the Run Log.

## Resume

Session stores only `active_run_id`. On startup Pico opens that Run Log, repairs an incomplete final line, reduces events into WorkingState/TaskState/Evidence, reconciles an unfinished Tool, rebuilds Context, resets the Provider session and continues with current Runtime configuration. A terminal Run Log clears `active_run_id`; stale pointers never override Run Log terminal state.

## Completion

Completion remains blocked by invalid Python syntax, failed current-workspace verification, unresolved partial/unknown effects, or unapplied implementation Child patches. A successful `run_shell` call whose command exactly matches the configured verifier is recorded against the current workspace fingerprint and reused by the Completion Gate. `pico run show` derives its summary directly from the Run Log.
