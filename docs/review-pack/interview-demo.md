# Pico interview demo

## 90-second path

1. Open `pico/agent_loop.py` and describe the loop as model proposal → staged admission → canonical outcome → optional policy hook → checkpoint → completion gate.
2. Open `pico/subagents/manager.py`: explicit DAG → parallel independent Children → isolated implementation Worktrees → receipt-bound Patch integration.
3. Run `uv run pytest -q tests/test_subagents.py`; show actual parallelism, failure propagation, session continuation, write-scope rejection and verified Patch application.
4. Run `uv run python scripts/demo_runtime.py`; point out `[false, true, true]` provider prompt reuse, Checkpoint v8, the changed-file evidence, and the empty pending-operation set.
5. Run `uv run python scripts/run_evaluations.py`; point out that the command fails closed and the Native Harness covers edit, recovery, safety, and governance.
6. Open `artifacts/runtime-policy-v1.json`: recovery advice, hook boundaries, structured verification, and the hash chain are replayable deterministic evidence.

## Five-minute deep dive

- Show `pico/providers/clients.py`: native Responses output items and matching function outputs remain in one task-local provider conversation.
- Show `pico/context_ledger.py`: the durable ledger is the reset/resume boundary, while immutable artifacts retain full audit output.
- Show `pico/mutations.py`: writes require the exact observed SHA-256 revision and commit through atomic replace.
- Show `pico/sandbox.py`: model-requested commands use direct argv in a no-network, read-only, resource-limited container.
- Show `pico/subagents/dag.py` and `pico/subagents/worktree.py`: dependency validation, overlapping-write rejection, exact-path authority, Patch digests and fail-closed integration.
- Show a Run's `events.jsonl` and explain sequence, causation, correlation, and hash-chain validation.

## Claims and boundaries

- Claim the deterministic 5/5 Harness, the 5/5 hidden preflight, the fixed five-task live suite, and the 25/25 official public assertions separately. All five live tasks completed on their first attempt; no task failure was selectively rerun.
- State that Pico is local and single-user; Docker daemon trust, remote multi-tenant isolation, distributed queues, and secret management are outside its boundary.
- State that Child execution is bounded and synchronous from the Parent tool's perspective; there is no background mailbox, recursive Agent tree or cross-process Child recovery.
