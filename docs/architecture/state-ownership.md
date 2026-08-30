# Runtime State Ownership

Pico has one durable source for each Run's process facts. Content state remains owned by Workspace, Project Memory, and Artifact storage.

Terminology in this document is strict:

- A **Fact** is an accepted Run Event persisted in `events.jsonl`.
- A **Projection** is rebuildable state reduced from Facts; it is never an additional source of truth.
- **Evidence** is the `RunEvidence` projection derived from Tool Result and Verification Facts; it
  exposes factual repair, effect and verification relationships but no completion decision.
- **Completion** is the ordered policy decision made only by `CompletionController`; terminal ownership begins only
  when `RunLifecycle` appends `assistant_final` or `run_stopped`.
- **Tool runtime** is the `ToolRuntime` public boundary backed by private tool-execution helpers,
  `ToolContext` and concrete Tool Runners. It owns no second durable Tool state.

| Scope | Source of truth | Derived state |
|---|---|---|
| Run | `events.jsonl` | One RunProjection: identity, TaskState, Evidence, Metrics, Pending Call and final Diff receipt |
| Session | `active_run_id` | RuntimeRecovery state |
| Task contract | First `user_message.contract` | Goal, task kind, write scope and completion requirements |
| Current task working state | Successful `update_working_state` Tool transactions | Constraints, decisions and next steps prompt section |
| Project | Markdown Memory Cards | Bounded `MEMORY.md` catalog plus explicit `memory_recall` Tool results |
| Large output | Artifact content + descriptor | Run Log reference |
| Subagents | Child Run Logs and Patch files | In-process DAG and applied flags |

## Ownership across the three current paths

| Path | Durable writes | Rebuildable/current state |
|---|---|---|
| CLI / resume | First `user_message.contract`, then Session `active_run_id`; `run_started` or `run_resumed`; interrupted reconciliation result when needed | RuntimeRecovery selection and one RunProjection replay |
| Normal Tool turn | `assistant_tool_call`, fsynced `tool_started`, then fsynced `tool_result` | ToolRuntime admission/orchestration, private tool-execution helpers, ToolContext-bound Tool Runner result, Projection and Evidence updates |
| Final submission | `model_instruction` + `completion_blocked` when rejected; otherwise `assistant_final` or `run_stopped` with only the `final_diff` receipt | Completion decision before settlement; terminal TaskLifecycle and final Diff reference after settlement |

Invariants:

- `tool_started` is durable before side effects begin.
- `tool_result` is the only durable completion fact for a Tool call.
- Context includes TaskContract, WorkingState, repository projections and Run Log facts; Prompt build is read-only.
- Live execution and replay use the same RunProjection reducer.
- Live code applies one Fact with `RunProjection.apply_event`; `RunStore.load_run` owns the single
  persisted read that returns Events plus Projection, and `RunStore.replay` is its Projection-only
  facade; `replay_events` is reserved for an already complete Event sequence.
- TaskContract, incremental WorkingState, TaskLifecycle, Evidence and Metrics can be rebuilt from the Run Log.
- Terminal events persist only the final Diff receipt, not a second copy of task status or evidence.
- Session `active_run_id` is an index pointer; Run Log terminal state is authoritative.
- Tool protocol events form one strict `assistant_tool_call -> tool_started? -> tool_result` transaction.
- Subagent scheduling and Patch application are synchronous and process-local; cross-process Child recovery is outside scope.
- Old persistence formats are rejected; no compatibility or migration branch exists.
