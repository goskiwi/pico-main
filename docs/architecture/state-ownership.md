# Runtime State Ownership

Pico keeps separate persistence surfaces because model context, crash recovery and audit evidence
have different trust and lifecycle requirements. Each fact still has one authoritative owner.

| State | Authoritative owner | Derived or cached forms |
|---|---|---|
| Tool execution, verification and terminal facts | `events.jsonl` | `task_state.json`, Evidence, Report |
| Current-run model-visible causality | `context.jsonl` | Token-budgeted prompt projection |
| Cross-run run summaries and working memory | Session JSON | Relevant-history prompt projection |
| Crash-resumable runtime envelope | Checkpoint | Rendered checkpoint prompt section |
| Large redacted tool output | Immutable artifacts | Context/Event artifact references |
| Project knowledge | Markdown Memory Cards | Generated `MEMORY.md` index |

## Invariants

- Event Log remains append-only, hash-chained and the source for run counters and terminal state.
- Context Ledger remains the source for current-run call/result pairing and model-visible history.
- TaskState is an operational cache. At terminal report generation it must match Event replay.
- Checkpoint stores only the latest executable recovery envelope and binds an Event cursor, Context
  and Workspace content. TaskState is reconstructed from Events rather than embedded in Checkpoint.
- Report and Evidence are projections. They may be regenerated from Events and immutable artifacts.
- Session stores only terminal run summaries across runs, including the original request, result,
  changed paths and verification status. Tool transcripts and Runtime guidance remain exclusively
  in the current Run's Context and Events.
- `MEMORY.md` and SQLite-style materialized views must never become independent facts.

## Deliberate Separation

Context and Events overlap on tool outcomes but must not be merged. Context is bounded and designed
for the model; Events are complete Runtime control facts. Workspace mutation, Project Memory,
Artifacts and Event append also retain their specialized write semantics rather than sharing the
ordinary replaceable-JSON helper.
