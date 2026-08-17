# Pico interview demo

## 90-second path

1. Open `pico/agent_loop.py` and describe the loop as model proposal → staged admission → canonical outcome → optional policy hook → checkpoint → completion gate.
2. Run `uv run python scripts/demo_runtime.py`; point out `[false, true, true]` provider prompt reuse, Checkpoint v6, the changed-file evidence, and the empty pending-operation set.
3. Run `uv run python scripts/run_evaluations.py`; point out that the command fails closed and the Native Harness covers edit, recovery, safety, and governance.
4. Open `artifacts/runtime-policy-v1.json`: recovery advice, hook boundaries, structured verification, and the hash chain are replayable deterministic evidence.

## Five-minute deep dive

- Show `pico/providers/clients.py`: native Responses output items and matching function outputs remain in one task-local provider conversation.
- Show `pico/context_ledger.py`: the durable ledger is the reset/resume boundary, while immutable artifacts retain full audit output.
- Show `pico/mutations.py`: writes require the exact observed SHA-256 revision and commit through atomic replace.
- Show `pico/sandbox.py`: model-requested commands use direct argv in a no-network, read-only, resource-limited container.
- Show a Run's `events.jsonl` and explain sequence, causation, correlation, and hash-chain validation.

## Claims and boundaries

- Claim the deterministic 5/5 Harness and the 5/5 fail-before/pass-after preflight separately. Only claim a live suite result when its artifact matches the current Runtime snapshot.
- State that Pico is local and single-user; Docker daemon trust, remote multi-tenant isolation, distributed queues, and secret management are outside its boundary.
