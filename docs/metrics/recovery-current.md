# Current Pico recovery evaluation

- Source: `225c0e4`; dirty=true
- Result: **12/12 passed**
- Duration: 0.194s
- Every row is a distinct recovery state; no repeated variants are used to inflate the count.

| Category | Scenario | Result |
|---|---|---|
| task | `pointed_active_run` | passed |
| task | `torn_log_tail` | passed |
| task | `damaged_checkpoint` | passed |
| task | `compacted_history_tail` | passed |
| task | `failure_state` | passed |
| tool | `before_started` | passed |
| tool | `started_file_unchanged` | passed |
| tool | `published_before_result` | passed |
| tool | `deleted_before_result` | passed |
| tool | `created_before_result` | passed |
| tool | `shell_effect_untracked` | passed |
| tool | `result_already_durable` | passed |
