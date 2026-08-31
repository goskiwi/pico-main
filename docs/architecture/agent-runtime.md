# Agent Runtime Architecture

Pico uses `events.jsonl` as the durable source for each Run's process Facts. One `RunProjection` reducer builds TaskState, RunEvidence, RunMetrics, the single Pending Call and final Diff receipt for live execution and replay. Workspace, RepoMap and Artifact stores remain authoritative for their current content.

## Current execution paths

### CLI startup and resume

```text
CLI build_agent
  -> Pico composition root
  -> load_resumable_run reads Session.active_run_id, or discovers an orphaned unfinished Run
  -> new: RunLifecycle appends user_message + TaskContract before the Session pointer
  -> resume: RunStore.load_run reads once, validates Run Log v15 / terminal Artifact,
     and returns that Event snapshot with its Projection
  -> RunLog.reconcile_interrupted resolves an unfinished Tool transaction without replay
  -> resumed input is appended as user_guidance before run_resumed; new Runs append run_started
  -> Provider action session reset
  -> AgentLoop
```

### Normal Tool turn

```text
AgentLoop
  -> PromptBuilder / ContextManager
  -> OpenAICompatibleModelClient -> ModelAction.tool
  -> append assistant_tool_call Fact
  -> ToolRuntime resolves the canonical pending ToolCall from that durable Fact
  -> ToolRuntime validates Registry / Surface / Schema / Policy / Approval
  -> ToolRuntime enforces protocol/repeat guards and captures effects/preimages
  -> private tool_execution helpers calculate preview/redaction/drift/diff/transitions/classification
  -> append + fsync tool_started Fact with planned effect paths
  -> ToolContext supplies bounded Workspace / Store / Run capabilities
  -> concrete Tool Runner -> ToolRunnerResult
  -> ToolRuntime uses those pure values to construct ToolOutcome
  -> append + fsync tool_result Fact
  -> RunProjection applies Fact and RunEvidence derives observations/effects
  -> bounded ToolOutcome returned to the Provider session
```

`ToolRuntime` is the one public boundary for model-visible tools. Stateless value helpers live in
the private `tool_execution.py` module; transaction order and persistence stay in `ToolRuntime`.
Concrete runners receive only a `ToolContext` rather than the whole Pico object.

### Final submission

```text
ModelAction.final
  -> CompletionController assesses TaskContract + Child receipts + Evidence + Verifier
  -> blocked: model_instruction + completion_blocked, then continue Provider
  -> allowed: RunLifecycle builds the net final Diff receipt
  -> append assistant_final Fact
  -> RunProjection becomes terminal
  -> clear Session.active_run_id and ExecutionContext

controlled stop
  -> RunLifecycle appends run_stopped + final_diff receipt
  -> workspace drift is reported as unavailable_reason, never as a trustworthy Diff
```

An active `reset()` first requests `user_reset` on the existing ExecutionContext and returns without replacing Run state. The running AgentLoop records the current Tool Result, observes cancellation, appends `run_stopped`, clears the Session pointer, and only then clears ActiveRunState. A dormant reset performs the same reconciliation and terminal settlement synchronously.

## Terminology

- **Fact**: an accepted durable Run Event in `events.jsonl`. Tool Call, resumed `user_guidance`, `tool_started`, Tool
  Result, Verification, Compaction and terminal events are Facts. Workspace files and Artifact
  contents remain facts of their own stores, not duplicated Run Facts.
- **Projection**: the rebuildable in-memory `RunProjection` produced by reducing Facts. It is not
  a second persistence format. Live execution calls `apply_event`; `RunStore.load_run` returns one
  validated Event snapshot and its Projection, while `RunStore.replay` delegates to it and returns
  only the Projection. Only callers that already own a complete Event sequence use `replay_events`.
- **Evidence**: `RunEvidence`, derived from Tool Result and Verification Facts. It contains
  observations, side effects, final net changes and verification records.
- **Completion**: the `CompletionController` decision about whether final submission is allowed.
  A Run is terminal only after `RunLifecycle` appends the corresponding terminal Fact.
- **Tool runtime**: the concrete `ToolRuntime` public boundary plus its private tool-execution helpers,
  `ToolContext` and concrete Tool Runners. It owns orchestration, not a second persistence format.

## State ownership

| State | Durable owner | Projection/cache |
|---|---|---|
| Current Run facts | `runs/<run_id>/events.jsonl` | One RunProjection containing identity, Task, Evidence, Metrics, Pending and final Diff receipt |
| Active Run pointer | Session `active_run_id` | The installed `ActiveRunState`; `resumable` is derived from Task + RunLog + terminal/execution state |
| Task contract | First `user_message.contract` | Goal, task kind, write scope and completion requirements |
| Current task working state | Successful `update_working_state` Tool transactions | Constraints, decisions and next steps prompt section |
| Large redacted output | Artifact files | Run Log artifact reference |
| Child receipts | Child Run Logs and Patch files | One explicit Child receipt and integration state |

`task_state.json`, `context.jsonl` and Checkpoint snapshots do not exist.

## Run events

Every event has one strict schema, contiguous sequence and stable ID:

```text
user_message
user_guidance
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

- stable call ID bound to the persisted `assistant_tool_call`;
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

Tool Runners return machine-readable facts plus `FailureInfo` through `ToolRunnerResult`. The durable failure stores one recovery condition (`retry_after_change`, `retry_after_wait`, `user_action_required`, or `no_retry`); Runtime derives the model-facing correction action instead of persisting a second decision. The repeat cache is only a process-local aid against immediately replaying a matching `partial/unknown` call; durable mutation safety remains owned by create-only writes, revisions, atomic stores and Patch state. Manual mode only permits observation tools; mutations require an active Run and exact persisted Pending Call. The effective parent write scope is the intersection of the persisted TaskContract and current Runtime policy: Implement delegation declarations and planned Patch integration paths are both checked against it before Approval or execution.

## Context and compaction

Stable role, execution, Tool protocol, WorkingState and completion rules are sent through
Responses `instructions`. The first request of a Provider action session sends a small dynamic
`input` with two always-present parts, Runtime task policy and the original task request. It adds an
untrusted-context envelope only when at least one bounded projection is non-empty, and adds a
differing latest request only on Resume. The envelope can include minimal Workspace facts and
document names, WorkingState,
`AGENTS.md` repository conventions, RepoMap and History. Empty RepoMap, WorkingState and History
sections are not rendered; project-document bodies are not preloaded.
Normal Tool continuation appends the native `function_call` and `function_call_output` to the same
manual Responses replay instead of rebuilding or resending the dynamic suffix.

Native Function Schemas remain in the Responses `tools` field rather than being copied into
instructions. AgentLoop resolves three surfaces for every fresh model turn:

- `declared_tools`: the complete Runtime-native action schemas;
- `allowed_tool_names`: the subset admitted by the current TaskContract, Runtime policy and Tool
  budget;
- `wire_tools`: the schemas actually used for Provider Token accounting and the request.

For a backend with verified `allowed_tools` support, `wire_tools` stays equal to the stable complete
schema set while `tool_choice.allowed_tools` carries the dynamic names during normal execution. The
final-only boundary is intentionally stricter: when only `submit_final` remains allowed, `wire_tools`
also shrinks to that one schema. On a backend without `allowed_tools`, the request always sends the
already-narrowed schemas. Response parsing and ToolRuntime both enforce the allowed-name subset.
Provider capabilities are selected explicitly by backend host; Pico does not probe production
requests or maintain a legacy Prompt/Tool protocol.

The stable instructions hash is also the `prompt_cache_key` on verified cache-capable backends.
Turn metrics distinguish `input_tokens`, `cached_tokens` and derived uncached input, so cache reuse
is measured rather than inferred. Context budgeting charges the actual
`wire_tools`, not an unrelated superset or subset.

Ordinary Call/Output continuation reuses the Provider session. If the wire or allowed Tool surface
changes—for example, the execution budget leaves only `submit_final`—AgentLoop resets that Provider
session and rebuilds from RunLog before the next request. Changing only the request field inside an
existing continuation is not treated as a reliable capability boundary.

Prompt construction is read-only. Before a fresh build, AgentLoop may explicitly prepare
Compaction: an isolated Provider session summarizes historical facts into exactly two semantic
sections, `Progress` and `Critical Context`. Its input is escaped compact JSON built from each
canonical Event payload, so ToolOutcome status, execution, side-effect, failure, path and artifact
facts stay together without opening a second trust boundary. The persisted Compaction Fact contains
that two-section summary plus coverage metadata; it never contains a seven-part generated summary.
TaskContract, WorkingState and RunEvidence keep their existing owners. A Summary is
committed only when replacing the covered prefix reduces the final escaped History Wire. Invalid,
failed or non-shrinking summaries commit no event and use a bounded suffix of complete Tool
Call/Result transactions. Semantic Summary uses one strict request; any failure takes that existing
fallback path. Transport retry remains Provider-owned. Original events remain durable.

Day 5 and the demo may assemble the following **Effective Recovery Context** for teaching and
observation. This is a read-only view over existing owners, not a second state object or an extra
Prompt payload:

| Effective category | Source |
|---|---|
| Goal | Immutable TaskContract from the first `user_message` |
| Constraints & Preferences | Run WorkingState constraints |
| Progress | Semantic `Progress` after Compaction, otherwise retained complete History facts |
| Key Decisions | Run WorkingState decisions |
| Next Steps | Run WorkingState next steps |
| Critical Context | Semantic `Critical Context` after Compaction, otherwise retained Tool Results/current data |
| Execution Evidence | RunEvidence projected from Tool Result and Verification Facts |

Only Progress and Critical Context can come from the semantic summarizer. The seven-category view is
not persisted, is not fed back as another seven-section suffix, and is not an input to completion.
`CompletionController` continues to decide from TaskContract, RunEvidence, Child integration state,
verification and the current Workspace.

The OpenAI-compatible adapter classifies structured HTTP/JSON/SSE context failures as
`ProviderContextOverflow` without retaining raw provider error objects or response bodies. AgentLoop
handles only that type: the first overflow resets the Provider session and rebuilds the Prompt; a
second consecutive overflow propagates. Other `RuntimeError` values never enter the overflow path.

## Resume

Session stores only `active_run_id`. On startup `load_resumable_run` opens that Run Log once, repairs an incomplete final line, reduces events through the same RunProjection used live, and installs the Projection and RunLog directly in `ActiveRunState`. `RunLifecycle` then reconciles an unfinished Tool, persists the new resume input as `user_guidance`, rebuilds Context from durable Facts, resets the Provider session and continues with current Runtime configuration. Resume must keep the persisted TaskContract requirements and write scope; current Tool policy may narrow that scope by intersection but never rewrites it. After any unhandled request exception, Pico reloads the current durable snapshot because the failed append may already have fsynced; a transient reload failure leaves the process-local `reload_required` cache-validity bit set so the next request retries before using Task state. A terminal Run Log clears `active_run_id`; an invalid non-empty pointer fails closed.

## Completion

`CompletionController` is the only completion-policy owner. It checks, in order: an unintegrated implement Child; the persisted TaskContract; unrepaired `unknown/partial` effects; external Workspace drift; required/current verification; and effects that remain unresolved after verification. This ordering prevents meaningless verifier runs for unrepaired uncertainty, while a repaired Workspace partial must receive a passing verifier for the current mutation/path state. `RunEvidence` only answers factual relationship queries such as repair and unresolved-effect lookup; it does not return a completion decision.

## Child delegation and integration

The Parent exposes two synchronous Tools. `delegate` creates exactly one Child with role `explore`
or `implement`; `integrate_child` accepts exactly one completed implementation receipt by child ID.
There is no batch request, dependency graph, background queue, or worker-pool scheduler.

An explore Child uses read-only Tools against the Parent Workspace and does not create a Worktree.
An implement Child must declare non-empty allowed write paths and always runs in a dedicated Git
Worktree rooted at the delegation base. Child Tool surfaces exclude `delegate`, so delegation cannot
recurse. The Parent receives a compact result plus a receipt; Child Tool history remains in the
Child Run Log.

Implementation completion never mutates the Parent automatically. `integrate_child` revalidates the
recorded base, applies the Patch in a temporary integration Worktree, runs the user-fixed
Verification there, and only then writes the verified result into the Parent Workspace. A base
change, path-scope mismatch, Patch failure, or failed Verification rejects integration without
claiming success.

Completed implementation receipts and their integration state are projected from the Parent Run
Log, so a restarted Parent can continue `integrate_child`. A Child that was still running at process
exit is not resumed or automatically dispatched again.

Read-only tasks need a successful Observation; modify tasks that require change need a non-empty final RunChangeSet. Each path stores one initial preimage, so `A -> B -> A` is touched but not a net change; successful settlement persists the actual final Unified Diff Artifact. External drift blocks successful completion. A user cancellation or reset can still terminalize safely, but its receipt explicitly records `unavailable_reason=workspace_drift` instead of claiming a trustworthy Diff. Verification freshness is derived from its mutation sequence and path states rather than stored as a mutable label. There is no implicit language-specific AST gate.

### Local verification trust boundary

The model has no general-purpose shell Tool. Verification is the one command-execution path: the
user supplies a fixed command explicitly, and the Completion boundary runs it from the Workspace
under the same operating-system account that launched Pico. The model cannot generate or alter
that command.

This is host execution, not a sandbox. Workspace path checks constrain Pico's structured file
Tools; they do not constrain the verifier's system calls, filesystem access, child processes, or
network access. Pico is therefore suitable only for repositories the user already trusts. Unknown
repositories and pull requests require an external CI runner, VM, or container boundary.

### Bounded workspace freshness design

Pico previously opened every non-ignored workspace file, computed a SHA-256 for each, and hashed the complete map before and after verification. That cost scales with total repository bytes and duplicated facts already owned by Git and the Run Log, so the full-workspace fingerprint path and API were removed. The current design reuses the existing per-path content state only for paths already established as changed by RunEvidence.

A Git worktree version remains a possible stronger contract if Pico later must detect IDE or other process changes anywhere in the repository at Completion. It would combine HEAD, staged index identity, porcelain-v2 status, dirty/untracked content revisions, explicit Runtime-affected ignored paths, and submodule state. It is not implemented now: it adds Git scans, non-Git fallback semantics, ignored-file and submodule edge cases beyond the concrete changed-path freshness contract. Stale writes remain protected separately by expected-revision checks at the file commit point.
