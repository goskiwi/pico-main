# Runtime State Ownership

Pico keeps separate persistence surfaces because model context, crash recovery and audit evidence
have different trust and lifecycle requirements. Each fact still has one authoritative owner.

| State | Authoritative owner | Derived or cached forms |
|---|---|---|
| Tool execution, verification and terminal facts | `events.jsonl` | `task_state.json`, Evidence, Report |
| Current-run model-visible causality | `context.jsonl` | Token-budgeted prompt projection |
| Cross-run conversation and working memory | Session JSON | Relevant-history prompt projection |
| Crash-resumable runtime envelope | Checkpoint | Rendered checkpoint prompt section |
| Large redacted tool output | Immutable artifacts | Context/Event artifact references |
| Project knowledge | Markdown Memory Cards | Generated `MEMORY.md` index |

## Invariants

- Event Log remains append-only, hash-chained and the source for run counters and terminal state.
- Context Ledger remains the source for current-run call/result pairing and model-visible history.
- TaskState is an operational cache. At terminal report generation it must match Event replay.
- Checkpoint stores the latest executable recovery envelope and binds an Event cursor, Context and
  Workspace content. It is not a second audit history.
- Report and Evidence are projections. They may be regenerated from Events and immutable artifacts.
- Session History is preserved across runs, but records created during a turn are flushed together
  with the next Checkpoint instead of being written independently.
- `MEMORY.md` and SQLite-style materialized views must never become independent facts.

## Deliberate Separation

Context and Events overlap on tool outcomes but must not be merged. Context is bounded and designed
for the model; Events are complete Runtime control facts. Workspace mutation, Project Memory,
Artifacts and Event append also retain their specialized write semantics rather than sharing the
ordinary replaceable-JSON helper.
