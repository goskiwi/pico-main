# Current Pico recovery evaluation

- Source: `5d9b5bf`; dirty=false
- Result: **13/13 passed**
- Duration: 0.201s
- Every row is a distinct recovery state; no repeated variants are used to inflate the count.

| Category | Scenario | Result |
|---|---|---|
| task | `pointed_active_run` | passed |
| task | `orphan_active_run` | passed |
| task | `torn_log_tail` | passed |
| task | `damaged_checkpoint` | passed |
| task | `compacted_history_tail` | passed |
| task | `runtime_feedback` | passed |
| tool | `before_intent` | passed |
| tool | `intent_file_unchanged` | passed |
| tool | `published_before_settlement` | passed |
| tool | `deleted_before_settlement` | passed |
| tool | `created_before_settlement` | passed |
| tool | `shell_effect_unknown` | passed |
| tool | `settlement_already_durable` | passed |
