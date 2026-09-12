"""Repeatable, non-gating measurements for RunLog append and recovery cost."""

import argparse
import json
import resource
import tempfile
import time
from pathlib import Path

from pico import context_manager
from pico.run_log import RunLog
from pico.run_store import RunStore
from pico.task_state import TaskContract, WriteScope


def elapsed(call):
    started = time.perf_counter()
    result = call()
    return time.perf_counter() - started, result


def measure(size):
    with tempfile.TemporaryDirectory() as directory:
        store = RunStore(Path(directory) / "runs")
        log = RunLog(f"run_{size}", "session_benchmark", store)
        log.append_user(TaskContract("benchmark", WriteScope("none"), False))

        append_seconds, _ = elapsed(
            lambda: [
                log.append_user_guidance(f"guidance {index}")
                for index in range(size - 1)
            ]
        )
        path = store.events_path(log.run_id)
        recovery_seconds, restored = elapsed(lambda: store.load_run(log.run_id))
        tokenizer = context_manager.Tokenizer("scripted-model")

        def build_context():
            history = restored.history()
            raw = {
                "runtime_policy": "runtime_policy: benchmark",
                "repository_instructions": "",
                "task_request": "task_request: benchmark",
                "runtime_instruction": "",
                "runtime_evidence": "",
                "latest_user_request": "",
                "workspace": "Workspace: benchmark",
                "history": context_manager.render_history(history),
            }
            rendered = context_manager._render_context(
                raw,
                8_000,
                section_caps=context_manager.DEFAULT_SECTION_CAPS,
                count_tokens=tokenizer.count,
                history=history,
            )
            return context_manager._assemble_input(raw, rendered)

        context_seconds, _context = elapsed(build_context)
        return {
            "events": size,
            "bytes": path.stat().st_size,
            "append_seconds": append_seconds,
            "context_seconds": context_seconds,
            "recovery_seconds": recovery_seconds,
            "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sizes", nargs="*", type=int, default=[1_000, 10_000, 100_000])
    args = parser.parse_args()
    for size in args.sizes:
        print(json.dumps(measure(size), sort_keys=True))


if __name__ == "__main__":
    main()
