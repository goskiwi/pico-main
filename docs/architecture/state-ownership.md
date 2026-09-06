# Runtime State Ownership

Pico has one durable source for each Run's process facts. Content state remains owned by Workspace and Artifact storage.

Terminology in this document is strict:

- A **Fact** is an accepted Run Event persisted in `events.jsonl`.
- A **Projection** is rebuildable state reduced from Facts; it is never an additional source of truth.
- **Evidence** is the `RunEvidence` projection derived from Tool Result and Verification Facts; it
  exposes historical effects, current tracked changes and verification records but no completion decision.
- **Completion** is the ordered policy decision made only by `CompletionController`; terminal ownership begins only
  when `RunLifecycle` appends `assistant_final` or `run_stopped`.
- **Tool runtime** is the `ToolRuntime` public boundary backed by private tool-execution helpers,
  `ToolContext` and concrete Tool Runners. It owns no second durable Tool state.

| Scope | Source of truth | Derived state |
|---|---|---|
| Run | `events.jsonl` | One RunProjection: identity, TaskContract, WorkingState, Evidence, Metrics, Pending Tool Call IDs and final Diff receipt |
| Session | `active_run_id` | The installed `ActiveRunState`; `resumable` is derived from Task, RunLog and execution state |
| Task contract | First `user_message.contract`, derived deterministically from the request and explicit Mode | Goal, maximum write capability, write scope and change-verification requirement |
| Resume guidance | Append-only `user_guidance` Events | Latest guidance is projected once as the mandatory latest request; older guidance remains History |
| Current task working state | Successful `update_working_state` Tool transactions | Constraints, decisions and next steps prompt section |
| Runtime correction | Latest structured `model_instruction` until an accepted Tool or terminal action | Mandatory trusted control plus untrusted evidence across Provider resets |
| Large output | Content Artifact or complete ToolOutcome model-envelope Artifact | Bounded model preview plus Run Log references |
| Child delegation | Child Run Logs and Patch files | One receipt per Child plus explicit integration state |

## Ownership across the three current paths

| Path | Durable writes | Rebuildable/current state |
|---|---|---|
| CLI / resume | First `user_message.contract`, then Session `active_run_id`; each resumed input as `user_guidance`; `run_started` or `run_resumed`; interrupted reconciliation result when needed | `load_resumable_run` installs one RunLog/RunProjection snapshot in ActiveRunState |
| Normal Tool turn | One ordered `assistant_tool_calls` containing one or more Calls, with per-Call fsynced `tool_started/tool_result` | ToolRuntime partitions calls by per-tool concurrency metadata; parallel-safe segments use bounded workers, exclusive calls form barriers, and durable state advances in original order |
| Final submission | `model_instruction` + `completion_blocked` when rejected; otherwise `assistant_final` or `run_stopped` with only the `final_diff` receipt | Completion decision before settlement; terminal status and final Diff reference after settlement |

Invariants:

- `tool_started` is durable before side effects begin.
- `tool_result` is the only durable completion fact for a Tool call.
- Context includes TaskContract, pending Runtime correction, WorkingState, repository projections and Run Log facts; Prompt build is read-only.
- Live execution and replay use the same RunProjection reducer.
- Live code commits one Fact with `RunLog.append`; `RunStore.load_run` owns the single
  persisted read that returns a ready RunLog plus Projection, and `RunStore.replay` is its Projection-only
  facade; `replay_events` is reserved for an already complete Event sequence.
- TaskContract, incremental WorkingState, terminal fields, Evidence and Metrics can be rebuilt from the Run Log.
- Terminal events persist only the final Diff receipt, not a second copy of task status or evidence.
- Session `active_run_id` is an index pointer; Run Log terminal state is authoritative.
- One model response remains an ordered durable envelope, while History and WorkingState project each completed Call independently. Each Call receives exactly one Result; event positions distinguish reused Provider call IDs across completed responses.
- Integration receipts describe confirmed patch application; an interrupted Tool can remain partial while its patch is confirmed applied and awaits current-state verification.
- Child delegation and integration are synchronous; there is no DAG/background scheduler. Completed Implement receipts and integration state replay from the Parent Run Log, but running Child execution is not resumed across processes.
- Old persistence formats are rejected; no compatibility or migration branch exists.
