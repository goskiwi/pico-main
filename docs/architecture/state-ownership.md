# Runtime State Ownership

Pico has one durable source for each Run's process facts. Content state remains owned by Workspace and Artifact storage.

Terminology in this document is strict:

- A **Fact** is an accepted Run Event persisted in `events.jsonl`.
- A **Projection** is rebuildable state reduced from Facts; it is never an additional source of truth.
- **Evidence** is the `RunEvidence` projection derived from Tool Result and Verification Facts; it
  exposes historical effects, current tracked changes and verification records but no completion decision.
- **Completion** is the read-only policy decision made only by `CompletionController`; `RunLifecycle`
  owns requested Verification execution and is the only writer of `assistant_final` or `run_stopped`.
- **Tool runtime** is the `ToolRuntime` public boundary backed by private tool-execution helpers,
  `ToolContext` and concrete Tool Runners. It owns no second durable Tool state.

| Scope | Source of truth | Derived state |
|---|---|---|
| Run | `events.jsonl` | One RunProjection: identity, TaskContract, WorkingState, Evidence, Metrics, Pending Tool Call IDs and final Diff receipt |
| Session | `active_run_id` repairable index | `ActiveRunState` stores one RunLog; its Projection is always derived from that RunLog |
| Task contract | First `user_message.contract`, derived deterministically from the request and explicit Mode | Goal, maximum write capability, write scope and change-verification requirement |
| Resume guidance | Append-only `user_guidance` Events | Latest guidance is projected once as the mandatory latest request; older guidance remains History |
| Current task working state | Successful `update_working_state` Tool transactions | Constraints, decisions and next steps prompt section |
| Runtime correction | Latest structured `model_instruction` until an accepted Tool or terminal action | Mandatory trusted control plus untrusted evidence across Provider resets |
| Execution lifecycle | One ExecutionContext deadline and cancellation token | Per-operation bounded timeouts; Provider requests, retry waits, Tools and Verification consume the same context |
| Provider continuation | Accepted parsed turn replay items plus their pending Call IDs | The raw Provider response is never a second interpretation or replay source |
| Context estimate | Provider-owned input usage plus accepted output usage for the latest response | Result serialization is added before continuation; after results are recorded or the session resets, estimation uses the actual local payload until new usage arrives. AgentLoop only consumes the estimate |
| Compacted history | Original Run Facts plus an optional derived Compaction Fact | Current-budget projection prefers a fitting Summary, otherwise omits it and selects recent complete transactions |
| Large output | One complete output Artifact; exact structured facts in ToolOutcome | Bounded model serialization from that same outcome |
| Child delegation | Child Run Logs and Patch files | One receipt per Child plus explicit integration state |

## Ownership across the three current paths

| Path | Durable writes | Rebuildable/current state |
|---|---|---|
| CLI / resume | First `user_message.contract`, then Session `active_run_id`; each resumed input as `user_guidance`; `run_started` or `run_resumed`; interrupted reconciliation result when needed | `load_resumable_run` installs one RunLog in ActiveRunState; Projection derives from it |
| Normal Tool turn | One ordered `assistant_tool_calls` containing one or more Calls, with per-Call fsynced `tool_started/tool_result` | ToolRuntime partitions calls by per-tool concurrency metadata; parallel-safe segments use bounded workers, exclusive calls form barriers, and durable state advances in original order |
| Final submission | `model_instruction` + `completion_blocked` when rejected; otherwise `assistant_final` or `run_stopped` with only the `final_diff` receipt | Completion decision before settlement; terminal status and final Diff reference after settlement |

Invariants:

- `tool_started` is durable before side effects begin.
- `tool_result` is the only durable completion fact for a Tool call.
- Context includes TaskContract, pending Runtime correction, WorkingState, repository projections and Run Log facts; Prompt build is read-only.
- Live execution and replay use the same RunProjection reducer.
- TaskContract stores one `write_scope`: `none`, `workspace`, or non-empty `paths`.
  Runtime intersects that original scope with current configuration on resume. The old
  contract fields `allows_workspace_mutation` and `allowed_write_paths` are rejected;
  the configuration and Child tool arguments still use `allowed_write_paths`.
- `RunLog.history()` constructs a read-only History snapshot with the current feedback
  event ID from RunProjection. History does not recompute feedback lifecycle rules.
  ToolContext declares concrete dependency types; optional executors remain optional.
- Run identity uses `run_id` and `session_id`; there is no independent Task ID.
  Old event envelopes containing `task_id` are rejected. `PendingToolGroup` owns batch
  calls, start/result progress and ordering checks; RunProjection delegates tool events
  to that object. Its internal counters are not Run-level fields.
- Live code commits one Fact with `RunLog.append`; `RunStore.load_run` owns the single
  persisted read that returns a ready RunLog owning its Projection, and `RunStore.replay` is its Projection-only
  facade; `replay_events` is reserved for an already complete Event sequence.
- TaskContract, incremental WorkingState, terminal fields, Evidence and Metrics can be rebuilt from the Run Log.
- Terminal events persist only the final Diff receipt, not a second copy of task status or evidence.
- Session `active_run_id` is a repairable index pointer; Run Log state is authoritative. Latest-resume
  discovery also scans Session-owned unfinished Runs and repairs a missing pointer.
- One accepted model response remains an ordered durable envelope, while History and WorkingState project each completed Call independently. The Provider parser rejects the entire response before replay or execution when any sibling is malformed. Each accepted Call receives exactly one Result; event positions distinguish reused Provider call IDs across completed responses.
- Execution cancellation and deadlines are never converted into Context or Compaction failures. Provider context estimates include only output committed to replay.
- Integration receipts describe confirmed patch application; an interrupted Tool can remain partial while its patch is confirmed applied and awaits current-state verification.
- Child delegation and integration are synchronous; there is no DAG/background scheduler. The Parent `tool_started` fact owns `ChildLaunch` before resource creation, so replay restores running identity. Recovery records that same Child as interrupted, cleans its planned Worktree and never adopts or reruns the Child. Implement Worktrees are disposable resources, not state owners.
- Old persistence formats are rejected; no compatibility or migration branch exists.
