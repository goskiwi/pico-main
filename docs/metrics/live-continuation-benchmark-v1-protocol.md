# Live continuation benchmark V1 protocol

This protocol measures two runtime claims with real model tool calls, while
keeping the model, prompts, fixtures, tool surface, and verifier fixed.

## What is frozen

- Manifest: `benchmarks/live_continuation_tasks_v1.json`
- Fixtures: `benchmarks/fixtures/live_continuation_v1/`
- Hidden verifier: `benchmarks/verifiers/live_continuation_v1/verify_output.py`
- Runner: `pico/evaluation/continuation_benchmark.py`
- CLI: `scripts/run_live_continuation_benchmark.py`

The result artifact stores fixture and evaluation SHA-256 snapshots. It does
not record credentials or the optional external `.env.local` path.

## Memory follow-up comparison

Each of the four memory tasks has two phases in a new fixture copy:

1. Phase one may only read the named source file and must return exactly
   `ACK`; it may not write a source value into checkpoint prose.
2. Phase two runs in a fresh model client and a fresh
   `Pico.from_session(...)` instance. It writes the source-derived value to a
   target file, then a hidden verifier checks exact output.

The paired variants differ only in `feature_flags.memory`:

| Variant | `memory` | `read_only_dedup` | Repo map | Context reduction | Dynamic budget | Prompt cache |
|---|---:|---:|---:|---:|---:|---:|
| `working_memory` | true | true | false | false | false | false |
| `memory_disabled` | false | true | false | false | false | false |

The primary efficiency measurement is the phase-two count of successful
physical access specs to the source file from the tool audit. It is reported
as a paired raw count, not as a forced or guaranteed cross-turn dedup result.
The benchmark also requires zero selected recent-run entries in the phase-two
prompt metadata, preventing recent run history from becoming a second fact
channel.

## Checkpoint resume scenarios

Each of the seven resume scenarios intentionally raises a benchmark-only error
on the next model call after a successful source `read_file` action. Pico has
already persisted the corresponding checkpoint before that call. Phase two is
then created with a fresh model client and `Pico.from_session(...)` from the
persisted session.

The suite checks expected resume status, stale paths, runtime-identity mismatch
fields, phase-two source-read requirements, hidden-verifier output, trace
parsing, and workspace isolation.

| Scenario family | Expected status |
|---|---|
| Clean checkpoint | `full-valid` |
| Key file replaced or deleted | `partial-stale` |
| Workspace README drift | `workspace-mismatch` |
| `max_new_tokens` or allowed-tool surface drift | `workspace-mismatch` |
| Legacy checkpoint schema | `schema-mismatch` |

The injected interruption contributes one expected phase-one model failure.
The artifact reports it separately from unexpected provider/model failures.

## Verification and publication rules

- Every episode receives an isolated fixture copy.
- A hidden verifier is copied in only after phase two stops; it is removed
  afterwards.
- The verifier runs only if the workspace-isolation audit passes.
- A clean published run uses `--require-clean-worktree` before result artifacts
  are generated.
- A 1× pilot validates transport, Docker, and all frozen acceptance checks.
  A 3× clean run is the publishable stability result.

Run a pilot with an explicit external credential file without copying secrets
into the benchmark worktree:

```bash
uv run python scripts/run_live_continuation_benchmark.py \
  --env-file /absolute/path/to/.env.local \
  --repetitions 1 \
  --artifact-path artifacts/live-continuation-pilot.json \
  --report-path artifacts/live-continuation-pilot.md
```

This is an engineering regression over controlled fixtures, not a broad
coding-capability benchmark. In particular, it must not be used to claim that
Pico's current single-turn read-only dedup cache persists across turns; it does
not. Any observed phase-two read reduction is attributable to the tested
session working-memory path under this frozen configuration.
