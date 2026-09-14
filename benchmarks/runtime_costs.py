"""Non-gating append, Context, full-Replay and Checkpoint measurements."""

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pico.compaction_summary import CompactedContext
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
    history = restored.history()
    messages = []
    compacted = history.compacted_message()
    if compacted is not None:
        messages.append(compacted)
    messages.extend(
        message
        for unit in history.model_message_units()
        for message in unit
    )
    return json.dumps(
        [message.to_dict() for message in messages],
        ensure_ascii=False,
        sort_keys=True,
    )


def build_fixture(root, size):
    store = RunStore(
        root,
        checkpoint_event_interval=size + 1,
        checkpoint_byte_interval=10**12,
    )
    log = RunLog(f"run_{size}", "session_benchmark", store)
    log.append_user(
        TaskContract(
            goal="benchmark",
            mode="ask",
            allowed_tools=("read_file",),
            write_scope=WriteScope("none"),
        )
    )
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
            log.append_compaction(
                CompactedContext(
                    constraints=(f"summary through event {sequence}",),
                    progress_done=(),
                    progress_in_progress=(),
                    progress_blocked=(),
                    key_decisions=(),
                    next_steps=(),
                    critical_context=(),
                    covered_through_sequence=(
                        log.context_state.recent_events[-1].sequence
                    ),
                )
            )
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
    guidance = [
        event.content
        for event in restored.history().recent_events()
        if event.kind == "user_guidance"
    ]
    return {
        "recovery_seconds": recovery_seconds,
        "context_seconds": context_seconds,
        "last_sequence": restored.projection.last_sequence,
        "user_guidance_count": len(guidance),
        "last_user_guidance": guidance[-1] if guidance else "",
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
            or full["user_guidance_count"] != checkpoint["user_guidance_count"]
            or full["last_user_guidance"] != checkpoint["last_user_guidance"]
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
