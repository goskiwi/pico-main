# Runtime State Ownership

Pico has one durable Run fact source.

| Scope | Source of truth | Derived state |
|---|---|---|
| Run | `journal.jsonl` | Context, TaskState, Evidence, Report, stats |
| Session | `active_run_id` and current task goal | Working-state prompt section |
| Project | Markdown Memory Cards | Bounded `MEMORY.md` catalog plus selected Card bodies |
| Large output | Artifact content + descriptor | Journal reference |
| Subagents | Parent `subtasks.json` and Child Journals | receipts and integration plan |

Invariants:

- `tool_started` is durable before side effects begin.
- `tool_result` is the only durable completion fact for a Tool call.
- Context includes only Journal facts and Compaction projections.
- TaskState and Evidence can be rebuilt from the Journal.
- Report generation never compares against a separately persisted mutable snapshot.
- Session `active_run_id` is an index pointer; Journal terminal state is authoritative.
- Old persistence formats are neither migrated nor deleted.
