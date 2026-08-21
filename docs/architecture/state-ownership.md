# Runtime State Ownership

Pico has one durable source for each Run's process facts. Content state remains owned by Workspace, Project Memory, and Artifact storage.

| Scope | Source of truth | Derived state |
|---|---|---|
| Run | `events.jsonl` | WorkingState, Context, TaskState, Evidence, Report, stats |
| Session | `active_run_id` | RuntimeRecovery state |
| Current task working state | `user_message` plus successful `update_working_state` Tool transactions | Goal, constraints, decisions, and next steps prompt section |
| Project | Markdown Memory Cards | Bounded `MEMORY.md` catalog plus explicit `memory_recall` Tool results |
| Large output | Artifact content + descriptor | Run Log reference |
| Subagents | Child Run Logs and Patch files | In-process DAG and applied flags |

Invariants:

- `tool_started` is durable before side effects begin.
- `tool_result` is the only durable completion fact for a Tool call.
- Context includes only Run Log facts and Compaction projections.
- WorkingState, TaskState and Evidence can be rebuilt from the Run Log.
- Report generation never compares against a separately persisted mutable snapshot.
- Session `active_run_id` is an index pointer; Run Log terminal state is authoritative.
- Tool protocol events form one strict `assistant_tool_call -> tool_started? -> tool_result` transaction.
- Subagent scheduling and Patch application are synchronous and process-local; cross-process Child recovery is outside scope.
- Old persistence formats are neither migrated nor deleted.
