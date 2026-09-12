"""Non-gating append, Context, full-Replay and Checkpoint measurements."""

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pico import context_manager
from pico.prompt_builder import _assemble_input, render_history
from pico.run_checkpoint import write_run_checkpoint
from pico.run_log import RunLog
from pico.run_store import RunStore
from pico.task_state import TaskContract, WriteScope

COMPACTION_INTERVAL = 1_000
TAIL_EVENTS = 100


def elapsed(call):
    started = time.perf_counter()
    result = call()
    return time.perf_counter() - started, result


def build_context(restored):
    tokenizer = context_manager.Tokenizer("scripted-model")
    history = restored.history()
    raw = {
        "runtime_policy": "runtime_policy: benchmark",
        "repository_instructions": "",
        "task_request": "task_request: benchmark",
        "runtime_instruction": "",
        "runtime_evidence": "",
        "latest_user_request": "",
        "workspace": "Workspace: benchmark",
        "history": render_history(history),
    }
    selected = context_manager.select_context(
        raw,
        8_000,
        section_caps=context_manager.DEFAULT_SECTION_CAPS,
        count_tokens=tokenizer.count,
        history=history,
        render_input=_assemble_input,
    )
    return _assemble_input(raw, selected)


def build_fixture(root, size):
    store = RunStore(
        root,
        checkpoint_event_interval=size + 1,
        checkpoint_byte_interval=10**12,
    )
    log = RunLog(f"run_{size}", "session_benchmark", store)
    log.append_user(TaskContract("benchmark", WriteScope("none"), False))
    checkpoint_at = max(1, size - min(TAIL_EVENTS, max(0, size - 1)))
    started = time.perf_counter()
    checkpoint_written = False
    while log.projection.last_sequence < size:
        sequence = log.projection.last_sequence
        if not checkpoint_written and sequence == checkpoint_at:
            offset = store.events_path(log.run_id).stat().st_size
            write_run_checkpoint(store.checkpoint_path(log.run_id), log, offset)
            checkpoint_written = True
        if (
            not checkpoint_written
            and sequence > 1
            and sequence % COMPACTION_INTERVAL == COMPACTION_INTERVAL - 1
        ):
            covered = [event.event_id for event in log.effective_history_events]
            log.append_compaction(f"summary through event {sequence}", covered)
        else:
            log.append_user_guidance(f"guidance {sequence}")
    if not checkpoint_written:
        offset = store.events_path(log.run_id).stat().st_size
        write_run_checkpoint(store.checkpoint_path(log.run_id), log, offset)
    return {
        "run_id": log.run_id,
        "append_seconds": time.perf_counter() - started,
        "bytes": store.events_path(log.run_id).stat().st_size,
        "checkpoint_bytes": store.checkpoint_path(log.run_id).stat().st_size,
    }


def load_measurement(root, run_id, mode):
    store = RunStore(root)
    if mode == "full":
        recovery_seconds, restored = elapsed(
            lambda: RunLog._from_events(
                store._read_events(run_id), store, expected_run_id=run_id
            )
        )
    else:
        recovery_seconds, restored = elapsed(lambda: store.load_run(run_id))
    context_seconds, rendered = elapsed(lambda: build_context(restored))
    return {
        "recovery_seconds": recovery_seconds,
        "context_seconds": context_seconds,
        "last_sequence": restored.projection.last_sequence,
        "latest_guidance": restored.history().latest_user_guidance(),
        "context_chars": len(rendered),
        "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def child_measurement(root, run_id, mode):
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--load-root",
            str(root),
            "--run-id",
            run_id,
            "--mode",
            mode,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def measure(size):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "runs"
        fixture = build_fixture(root, size)
        full = child_measurement(root, fixture["run_id"], "full")
        checkpoint = child_measurement(root, fixture["run_id"], "checkpoint")
        if (
            full["last_sequence"] != checkpoint["last_sequence"]
            or full["latest_guidance"] != checkpoint["latest_guidance"]
            or full["context_chars"] != checkpoint["context_chars"]
        ):
            raise RuntimeError("full and Checkpoint recovery diverged")
        return {
            "events": size,
            **fixture,
            "full": full,
            "checkpoint": checkpoint,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sizes", nargs="*", type=int, default=[1_000, 10_000, 100_000])
    parser.add_argument("--load-root")
    parser.add_argument("--run-id")
    parser.add_argument("--mode", choices=("full", "checkpoint"))
    args = parser.parse_args()
    if args.load_root:
        print(
            json.dumps(
                load_measurement(Path(args.load_root), args.run_id, args.mode),
                sort_keys=True,
            )
        )
        return
    for size in args.sizes:
        print(json.dumps(measure(size), sort_keys=True))


if __name__ == "__main__":
    main()
