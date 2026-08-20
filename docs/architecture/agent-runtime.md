# Agent Runtime Architecture

Pico uses one durable fact source per Run: `journal.jsonl`. Runtime objects are in-memory projections, not independent persisted state.

```text
User request
  -> Run Journal user_message
  -> bounded Context projection + RepoMap + Memory
  -> Responses ModelAction
  -> admission / approval
  -> assistant_tool_call
  -> fsynced tool_started with potential effects
  -> ToolExecution / ToolOutcome
  -> fsynced tool_result
  -> TaskState + Evidence projection
  -> verification_result
  -> assistant_final or run_stopped
```

## State ownership

| State | Durable owner | Projection/cache |
|---|---|---|
| Current Run facts | `runs/<run_id>/journal.jsonl` | TaskState, Evidence, Context, Report |
| Active Run pointer | Session `active_run_id` | RuntimeRecovery state |
| Current task goal | Session Working Memory | One prompt section |
| Project memory catalog | Generated `MEMORY.md` | Bounded resident section |
| Retrieved project knowledge | Selected Markdown Memory Cards | Independent high-priority section |
| Large redacted output | Artifact files | Journal artifact reference |
| Subtask graph | `subtasks.json` | Subagent scheduler state |

`task_state.json`, `context.jsonl`, `events.jsonl`, `report.json` and Checkpoint snapshots do not exist.

## Journal entries

Every entry has one strict schema, contiguous sequence and stable ID:

```text
run_started / run_resumed
user_message
model_requested / turn_metrics
assistant_tool_call
tool_started
tool_result
guidance / policy_decided
memory_selection
provider_session_reset
verification_started / verification_result
compaction
completion_blocked
assistant_final / run_stopped
```

The Journal is single-writer and fsynced after every accepted entry. It has no hash chain: a hash stored beside mutable local data is not a trusted tamper boundary. Strict schema, sequence and IDs detect structural corruption. A final incomplete JSONL tail is a crash artifact and is truncated; malformed complete entries fail closed.

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

## Context and compaction

The model Context uses the configured provider window and includes every active context-bearing Journal entry. Compaction starts only when total context crosses the window minus its output reserve, keeps a recent token budget without splitting Tool Call/Result units, and replaces an exact older prefix with Runtime Facts plus a structured semantic summary. Original entries remain in the Journal. Provider continuation metrics remain as `turn_metrics` so evaluation and reports retain prompt reuse, token and latency evidence without creating a second fact source.

## Resume

Session stores only `active_run_id`. On startup Pico opens that Journal, repairs an incomplete final line, reduces entries into TaskState/Evidence, reconciles an unfinished Tool, rebuilds Context, resets the Provider session and continues with current Runtime configuration. A terminal Journal clears `active_run_id`; stale pointers never override Journal terminal state.

## Completion

Completion remains blocked by invalid Python syntax, failed current-workspace verification, unresolved partial/unknown effects, or unapplied implementation Child patches. Reports are generated on demand from the Journal and cannot diverge from persisted Run facts.
