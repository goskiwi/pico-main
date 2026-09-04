# Agent Runtime Architecture

Pico uses `events.jsonl` as the durable source for each Run's process Facts. One `RunProjection` reducer builds TaskState, RunEvidence, RunMetrics, one Pending Tool transaction and the final Diff receipt for live execution and replay. A transaction is either one ordinary Call or one ordered pure-Observation Batch. Workspace, RepoMap and Artifact stores remain authoritative for their current content.

## Current execution paths

### CLI startup and resume

```text
CLI build_agent
  -> Pico composition root
  -> load_resumable_run reads Session.active_run_id, or discovers an orphaned unfinished Run
  -> new: explicit Ask/Code/Auto Mode deterministically creates and appends TaskContract
  -> resume: RunStore.load_run reads once, validates the Run Log / terminal Artifact,
     and returns that Event snapshot with its Projection
  -> resume: persisted TaskContract keeps the maximum capability; current Mode may narrow it
  -> RunLog.reconcile_interrupted resolves an unfinished Tool transaction without replay
  -> resumed input is appended as user_guidance before run_resumed; new Runs append run_started
  -> Provider action session reset
  -> AgentLoop
```

Ask exposes observations only. Code exposes the full configured surface and asks before risky
actions. Auto approves bounded file/Worktree mutations but omits `run_command` because Pico has no
Shell sandbox or secondary safety classifier. One active ask/resume is bounded by an Agent Turn
ceiling and one monotonic Turn deadline.

### Normal Tool turn

```text
AgentLoop
  -> PromptBuilder
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

### Observation Batch

One Provider response may contain two to four independent `list_files`, `read_file`, `search`, or
`read_artifact` calls. AgentLoop persists the complete ordered `assistant_tool_batch` first.
ToolRuntime performs whole-batch policy, budget, surface and argument preflight before execution.
Any execution, mutation, state, orchestration or completion call makes the whole Batch rejected;
every Call still receives one `rejected/not_started/none` Result.

For an accepted Batch, the main thread writes all `tool_started` Facts in original order. A bounded
worker pool runs only the raw read-only Runners with per-Call ToolContext values. The main thread
then performs redaction, Artifact materialization, Projection updates and `tool_result` appends in
the original Call order. All native `function_call_output` values return to the Provider in one
continuation. A failed Observation does not cancel valid siblings.

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
  -> workspace drift leaves stopped final_diff absent, never fabricates a trustworthy Diff
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
| Task contract | First `user_message.contract` | Goal, maximum write capability, write scope and change-verification requirement |
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
assistant_tool_batch
tool_started
tool_result
model_instruction
provider_session_reset
verification_result
compaction
completion_blocked
assistant_final / run_stopped
```

The Run Log is single-writer and fsynced after every accepted event. One main Pico process owns one
Workspace; independent concurrent modification tasks require separate Worktrees rather than shared
JSONL writers. It has no hash chain: a hash stored beside mutable local data is not a trusted tamper
boundary. Contiguous sequence and IDs plus strict User, Tool Call/Started/Result, and terminal
payloads reject causal corruption; telemetry payloads remain extensible. A final incomplete JSONL
tail is a crash artifact and is truncated; malformed complete events fail closed.

`turn_metrics` persists only Provider-reported `input_tokens` and `output_tokens`. Detailed Prompt
section accounting remains an in-memory build/debug value; Compaction and Provider reset have their
own causal Events and are not duplicated into per-turn telemetry.

Replaceable snapshots and Workspace mutations use same-directory temporary files plus atomic replace. `write_file` only creates an absent file; `edit_file` only changes an existing file and stages/fsyncs the complete payload before revalidating its expected revision at the `os.replace` commit point. A stale Revision preserves external content and returns expected/actual revisions plus ready-to-call `read_file` arguments. Missing text returns a bounded closest current excerpt when one is useful; ambiguous text returns bounded exact line ranges. These are facts on the existing ToolOutcome, not additional Tools or recovery state.

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

For an interrupted Observation Batch, recovery preserves an existing Result prefix, marks every
started suffix Call `operation_interrupted/none`, and marks every unstarted suffix Call
`operation_not_started/none`. It never replays the Runner. The whole Batch, including every Result,
is one indivisible Compaction/recent-history unit.

ToolOutcome keeps three explicit state dimensions for direct inspection:

- `status`: overall result (`success`, `error`, `rejected`, or `partial_success`);
- `execution_state`: whether the Tool Runner was not started, returned normally, or failed/interrupted;
- `side_effect_state`: whether effects are absent, changed, partial, or unknown.

Tool Runners return machine-readable facts plus `FailureInfo` through `ToolRunnerResult`. The durable failure stores one recovery condition (`retry_after_change`, `retry_after_wait`, `user_action_required`, or `no_retry`); Runtime derives the model-facing correction action instead of persisting a second decision. The repeat cache is only a process-local aid against immediately replaying a matching `partial/unknown` call; durable mutation safety remains owned by create-only writes, revisions, atomic stores and Patch state. Ask mode only permits observation tools; mutations require an active Run and exact persisted Pending Call. The effective parent write scope is the intersection of the persisted TaskContract and current Runtime policy: Implement delegation declarations and planned Patch integration paths are both checked against it before Approval or execution.

## Context and compaction

Stable role, execution, Tool protocol, WorkingState and completion rules are sent through
Responses `instructions`. The first request of a Provider action session sends a small dynamic
`input` with two always-present parts, Runtime task policy and the original task request. Applicable
`AGENTS.md` files from repository root through the invocation CWD are inserted between them as a
dedicated `repository_instructions` block. They are project instructions rather than system policy
or ordinary repository data: the current user request wins on conflict, and ToolRuntime remains the
permission boundary. An untrusted-context envelope is added only when at least one bounded
projection is non-empty, and a differing latest request is added only on Resume. The envelope can
include minimal Workspace facts, RepoMap, History and WorkingState. RepoMap is
ranked from the immutable goal, latest request, current WorkingState and paths already observed or
changed by the Run. History is rendered before WorkingState so an older summary cannot displace the
current constraints, decisions and next steps. The original task precedes this recovery context;
only differing Resume guidance is appended last.
Empty repository instructions, RepoMap, WorkingState and History sections are not rendered;
ordinary project-document bodies are not preloaded. Repository instructions have one 32 KiB total
byte limit and stay outside History compaction.
Normal Tool continuation appends native `function_call` and `function_call_output` items to the same
manual Responses replay instead of rebuilding or resending the dynamic suffix. Observation Batch
Results are appended together in original Call order before the next Provider request.

Native Function Schemas remain in the Responses `tools` field rather than being copied into
instructions. For every fresh model turn, AgentLoop computes the schemas currently admitted by the
TaskContract, Runtime policy and Tool budget, sends exactly that surface, and charges it to the
Context budget. Response parsing and ToolRuntime both enforce the same current surface; Pico does
not maintain a backend-host capability matrix or Prompt-cache protocol.

Ordinary Call/Output continuation reuses the Provider session. If the Tool surface
changes—for example, the execution budget leaves only `submit_final`—AgentLoop resets that Provider
session and rebuilds from RunLog before the next request. Changing only the request field inside an
existing continuation is not treated as a reliable capability boundary.

After a complete Action transaction, Provider-reported `input_tokens` is compared directly with
`provider_context_limit_tokens - compaction_reserve_tokens`. Reaching that high watermark resets the
continuation and lets the next fresh Prompt compact/rebuild from RunLog. Missing usage causes no
prediction; output tokens, local Tool Result size and `max_new_tokens` are not combined into a
synthetic next-request estimate. A typed Provider context overflow still performs one reset,
Compaction attempt and retry; a second consecutive overflow propagates.

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

`CompletionController` is the only completion-policy owner. It checks, in order: an unintegrated implement Child; unrepaired `unknown/partial` effects; the persisted maximum capability and no-change Observation requirement; external Workspace drift; change-triggered/current verification; and effects that remain unresolved after verification. A zero-change completion needs at least one successful Observation. A repaired Workspace partial must receive a passing verifier for the current mutation/path state. Completion establishes evidence sufficiency and freshness, not arbitrary business-semantic correctness. `RunEvidence` only answers factual relationship queries; it does not return a completion decision.

Completion returns one tagged `CompletionDecision(status, content)`: `allowed` carries the final
answer; a blocker status carries repair guidance. A successful terminal event requires
`FinalDiff(artifact_id, size_bytes)`, including an empty receipt for a confirmed zero-change result.
A stopped event may omit `final_diff`; its RunOutcome then exposes `None` rather than an error
string embedded inside an artifact descriptor.

## Coding application Git delivery

`applications.coding.CodingWorkflow` is an opt-in delivery application, not part of Pico Core. It
runs the normal AgentLoop through terminal Completion and obtains net changed
paths, and only then creates one Git commit restricted to those paths. It never commits a path that
was already dirty when the application started, never includes unrelated staged changes, and never
resets or pushes. It rechecks the existing RunChangeSet immediately before delivery. The commit
bypasses Git hooks because CodingWorkflow requires Runtime Verification and a hook must not mutate the
settled Final Diff. A stopped Run, an empty net change, a dirty-path overlap, or a Git failure
returns an explicit skipped/failed delivery result
while preserving the Workspace.

## Child delegation and integration

The Parent exposes two synchronous Tools. `delegate` creates exactly one Child with role `explore`
or `implement`; `integrate_child` accepts exactly one completed implementation receipt by child ID.
There is no batch request, dependency graph, background queue, or worker-pool scheduler.

An explore Child uses Ask-mode Tools against the Parent Workspace and does not create a Worktree.
An implement Child must declare non-empty allowed write paths and always runs in a dedicated Git
Worktree rooted at the delegation base. Child Tool surfaces exclude `delegate`, so delegation cannot
recurse. The Parent receives a compact result plus a receipt; Child Tool history remains in the
Child Run Log.

`ChildRecord.result` is `None` while running, `ChildSuccess` on completion, or `ChildFailure` on
failure. Only a successful implementation carries a `ChildPatch`; Explore and failure receipts do
not emit empty Patch fields. Failed receipts retain a Child Run ID when execution started.

Implementation completion never mutates the Parent automatically. `integrate_child` revalidates the
recorded base, applies the Patch in a temporary integration Worktree, runs the Runtime-configured
Verification there, and only then writes the verified result into the Parent Workspace. A base
change, path-scope mismatch, Patch failure, or failed Verification rejects integration without
claiming success.

Completed implementation receipts and their integration state are projected from the Parent Run
Log, so a restarted Parent can continue `integrate_child`. A Child that was still running at process
exit is not resumed or automatically dispatched again.

Child Agent turns and Integration verification inherit the Parent ExecutionContext's absolute Turn deadline; they never receive a fresh full timeout. Explore and Implement Children also have smaller Agent/Tool ceilings. A zero-change result needs a successful Observation. Each path stores one initial preimage, so `A -> B -> A` is touched but not a net change; successful settlement persists the actual final Unified Diff Artifact. External drift blocks successful completion. A user cancellation or reset can still terminalize safely, but a stopped receipt omits `final_diff` when no trustworthy Diff can be produced. Verification freshness is derived from command identity, mutation sequence and changed-path states rather than stored as a mutable label.

### Local verification trust boundary

In Code mode the model may request `run_command` for user-approved diagnostics such as tests, linters, type
checks, Git status/diff and reproductions. It is expected not to modify repository files; Runtime
captures repository-visible state before and after, and any change becomes an unresolved `unknown`
effect. Here `unknown` means the change lacks a trustworthy Run-start preimage, not that its paths
are necessarily unknown. Mutating Shell is not supported by this Runtime.
Ask and Auto do not expose `run_command`. The user separately supplies a fixed Verification command,
which the Completion boundary owns and the model cannot alter.

This is host execution, not a sandbox. Workspace path checks constrain Pico's structured file
Tools; they do not constrain diagnostic or verifier system calls, filesystem access, child processes,
or network access. Pico is therefore suitable only for repositories the user already trusts. Unknown
repositories and pull requests require an external CI runner, VM, or container boundary.

### Repository-visible freshness design

`run_command` and final Verification share one transient Repository snapshot. In Git repositories it
compares HEAD identity, staged and total binary diffs, and content revisions for non-ignored
untracked paths. This detects an already-dirty file changing again, index-only changes and HEAD
movement without requiring a clean workspace. Diff observation disables external diff and textconv
drivers. The fingerprint is not persisted; Events keep only the result and a bounded changed-path
preview.

Git-ignored files, `.git` metadata other than HEAD/ref, Workspace-external files, network effects and
background processes after command return are outside this observation contract. Non-Git
workspaces use a bounded metadata snapshot. A missing pre-execution baseline prevents command
execution; a missing post-execution snapshot cannot establish `none`. Stale structured writes remain
protected separately by expected-revision checks at their file commit point.
