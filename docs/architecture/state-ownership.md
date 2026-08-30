# Runtime State Ownership

Pico has one durable source for each Run's process facts. Content state remains owned by Workspace, Project Memory, and Artifact storage.

| Scope | Source of truth | Derived state |
|---|---|---|
| Run | `events.jsonl` | One RunProjection: identity, TaskState, Evidence, Metrics, Pending Call and final Diff receipt |
| Session | `active_run_id` | RuntimeRecovery state |
| Task contract | First `user_message.contract` | Goal, task kind, write scope and completion requirements |
| Current task working state | Successful `update_working_state` Tool transactions | Constraints, decisions and next steps prompt section |
| Project | Markdown Memory Cards | Bounded `MEMORY.md` catalog plus explicit `memory_recall` Tool results |
| Large output | Artifact content + descriptor | Run Log reference |
| Subagents | Child Run Logs and Patch files | In-process DAG and applied flags |

Invariants:

- `tool_started` is durable before side effects begin.
- `tool_result` is the only durable completion fact for a Tool call.
- Context includes TaskContract, WorkingState, repository projections and Run Log facts; Prompt build is read-only.
- Live execution and replay use the same RunProjection reducer.
- TaskContract, incremental WorkingState, TaskLifecycle, Evidence and Metrics can be rebuilt from the Run Log.
- Terminal events persist only the final Diff receipt, not a second copy of task status or evidence.
- Session `active_run_id` is an index pointer; Run Log terminal state is authoritative.
- Tool protocol events form one strict `assistant_tool_call -> tool_started? -> tool_result` transaction.
- Subagent scheduling and Patch application are synchronous and process-local; cross-process Child recovery is outside scope.
- Old persistence formats are rejected; no compatibility or migration branch exists.
