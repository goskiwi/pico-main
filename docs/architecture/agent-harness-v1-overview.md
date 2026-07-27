# Agent Harness v1 Overview

`pico` is organized around a small agent harness that turns one user request into a bounded sequence of model calls and tool executions.

```text
user request
  -> CLI builds Pico runtime
  -> task state and run directory are created
  -> RepoMap refreshes the task-ranked Python symbol graph
  -> ContextManager budgets the map and builds the prompt
  -> model client returns a normalized ModelAction
  -> a transient LangGraph routes model, tool, retry, and final transitions
  -> provider adapter replays native tool-call and tool-result messages
  -> tool validation, approval, and execution run
  -> changed paths are attached to the run undo journal
  -> session, trace, task state, and report are persisted
```

The runtime keeps the task state separate from the session. The session stores recoverable conversation and memory, while task state records the status of a single `ask()` run: attempts, tool steps, last tool, stop reason, checkpoint id, and final answer. LangGraph is deliberately an in-process router here; Pico's existing `TaskState`, checkpoints, trace, and reports remain the durable audit and resume boundary.

Session memory is deliberately a small working index: recent paths, short
file summaries bound to file-content hashes, and bounded process notes for
errors or rejected actions. Read results never also become notes, so stale
source facts have one freshness-checked representation. A session keeps only
its latest resumable checkpoint; earlier task history belongs to the run
artifacts that produced it.

## Interactive session and task boundary

The CLI is a stateful REPL: one runtime remains alive across user inputs and
persists session memory, durable checkpoints, and workspace facts. Each input,
however, starts one bounded coding task. Its provider action conversation, tool
trace, run directory, and undo journal are task-scoped; Pico resets the provider
action session before the next task. This keeps raw tool transcripts from
silently accumulating across an interactive session while preserving the
structured state needed for a later request to continue work.

```text
REPL session (persistent across user inputs)
  memory / durable checkpoints / workspace facts
      -> each user input
task (bounded and isolated)
  provider action conversation / tool trace / undo journal
      -> input threshold or provider overflow
  structured task-context checkpoint -> reset provider action session -> continue task
```

Repository retrieval is a separate layer. Tree-sitter extracts Python modules,
classes, functions, and methods; import, call, inheritance, containment, and test
relations form a weighted graph. Query identifiers seed Personalized PageRank, and
the ranked signatures enter a dedicated ContextManager budget. The same graph is
available through the read-only `query_repo_map` tool, which refreshes changed files
before answering. Ranking evidence is stored in prompt metadata and `report.json`.

The tool layer is intentionally explicit. Pydantic argument models are the single schema source for prompt signatures, local validation, tool identity, and provider-native function definitions. The model can only request registered tools, and risky tools still pass through Pico's approval and workspace-diff accounting. Shell commands also pass a dangerous-command screen before execution.

Risky workspace actions also pass through `run_undo.py`. Path-specific tools stage
their target and its parent directories; shell actions stage the workspace scope
because their output paths are not knowable in advance. Only paths that actually
change retain blobs. The first preimage remains stable across later edits in the
same run, while the expected post-state advances after every tool. `pico undo`
preflights every current path against that post-state before restoring anything,
so post-run user edits cause an all-or-nothing refusal. This restores an already
dirty worktree without changing Git refs or the index.

Delegation has a separate boundary: `tools.py` validates requests and renders outcomes, while `DelegateScheduler` reserves the batch step budget and executes workspace-read-only `run_delegate_child` calls through a bounded thread pool. Single and multi-delegate requests therefore share the same concurrency, timeout, and outcome accounting. Child sessions are marked as delegate audit artifacts and excluded from the default `--resume latest` path; concurrent run-index updates use a shared lock plus atomic replacement so sibling completions cannot overwrite one another.

## Narrow model boundary

Pico targets the GPT-5.6 Luna Responses contract, not a multi-provider agent
platform. `OpenAICompatibleModelClient` isolates LangChain transport and native
Responses items from the runtime, then normalizes strict function calls to
`ModelAction(tool | final | retry)`. That is a protocol seam for testability and
auditability, not a provider catalog: Pico does not negotiate provider
capabilities, route requests between models, or translate across model APIs.

Every workspace tool and `submit_final` are exposed through the Responses API's strict function definitions. The adapter replays encrypted reasoning items, function calls, and matching function outputs. It normalizes calls at Pico's provider boundary to `ModelAction(tool | final | retry)`; rejected actions are recorded in both trace and report artifacts.

Long tool conversations use task-context compaction rather than a one-shot overflow fallback. Pico measures provider-reported input tokens after each action; at the configured threshold, or immediately after an explicit provider overflow, it asks the same model for a structured checkpoint. The checkpoint records goal, constraints, progress, workspace fingerprint, verifier records, next step, and artifact references. Pico persists it in `context_compactions.jsonl`, retains a bounded tail of original tool evidence, resets the provider action session, and rebuilds the prompt from the checkpoint plus fresh Repo Map/workspace state. Compaction is bounded per task; a failed checkpoint is surfaced as a stopped run, never silently replaced with a lossy fallback.

Run observability is split into two artifacts:

- `trace.jsonl` is the detailed timeline for debugging a run.
- `report.json` is the final summary for review, metrics, and benchmark evidence.

`pico runs` and the REPL's `/runs` read the run index without constructing a
model client or creating a new artifact. They display only top-level main runs,
their current status, and Undo eligibility; delegate runs remain available in
their own artifacts for audit rather than becoming user-facing conversation
branches.
