"""Deterministic recovery evaluation for the current Pico event protocol."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from pico import (
    AssistantTurn,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    TaskContract,
    ToolCall,
    ToolOutcome,
    Workspace,
    WriteScope,
)
from pico.compaction_summary import CompactedContext
from pico.mutations import file_revision
from pico.run_log import RunLog
from pico.run_store import RunStore


class NoopModel:
    model = "recovery-eval"
    context_window_tokens = 272_000

    @staticmethod
    def reset_action_session():
        return None


def _revision():
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )
    return {"commit": commit or "unknown", "dirty": dirty}


def _new_run(root: Path, *, checkpoint_interval=10_000):
    sessions = SessionStore(root / ".pico" / "sessions")
    session = sessions.create(root)
    run_root = sessions.directory(session.id) / "runs"
    store = RunStore(
        run_root,
        checkpoint_event_interval=checkpoint_interval,
        checkpoint_byte_interval=10**9,
    )
    log = RunLog("run_eval", session.id, store)
    log.append_user(
        TaskContract(
            goal="Evaluate recovery",
            mode="auto",
            allowed_tools=("read_file", "write_file", "run_shell"),
            write_scope=WriteScope("workspace"),
        )
    )
    store.checkpoint_if_due(log, force=True)
    session.set_active_run(log.run_id)
    return sessions, session, store, log


def _restore(root: Path, sessions: SessionStore, session_id: str):
    session = sessions.load(session_id)
    return Pico(
        NoopModel(),
        Workspace.build(root, repo_root_override=root),
        session=session,
        config=PicoConfig(mode="auto"),
    )


def _task_case(name, setup):
    with tempfile.TemporaryDirectory(prefix=f"pico-{name}-") as directory:
        root = Path(directory)
        sessions, session, store, log = _new_run(root, checkpoint_interval=1)
        expected = setup(root, session, store, log)
        restored = _restore(root, sessions, session.id)
        actual = {
            "run_id": restored.run.projection.run_id,
            "summary": restored.run.projection.summary(),
            "history": restored.run.run_log.history().render_projection(),
            "user_messages": list(restored.run.run_log.history().user_texts()),
        }
        if actual != expected:
            raise AssertionError(f"{name}: restored state differs from durable oracle")


def _task_cases():
    def pointed(_root, _session, _store, log):
        log.append_user_guidance("pointed tail")
        return _oracle(log)

    def torn(_root, _session, store, log):
        log.append_user_guidance("durable before torn tail")
        expected = _oracle(log)
        with store.events_path(log.run_id).open("ab") as handle:
            handle.write(b'{"torn":')
        return expected

    def damaged_checkpoint(_root, _session, store, log):
        log.append_user_guidance("checkpointed")
        expected = _oracle(log)
        store.checkpoint_path(log.run_id).write_text("{broken", encoding="utf-8")
        return expected

    def compacted(_root, _session, _store, log):
        first = log.append_user_guidance("old context")
        log.append_compaction(
            CompactedContext(
                constraints=("old context summarized",),
                progress_done=(),
                progress_in_progress=(),
                progress_blocked=(),
                key_decisions=(),
                next_steps=(),
                critical_context=(),
                covered_through_sequence=first.sequence,
            )
        )
        log.append_user_guidance("tail after compaction")
        return _oracle(log)

    def failure_state(_root, _session, _store, log):
        log.append("model_requested")
        log.append_model_failure("protocol_error", "malformed", "malformed", {})
        return _oracle(log)

    return {
        "pointed_active_run": pointed,
        "torn_log_tail": torn,
        "damaged_checkpoint": damaged_checkpoint,
        "compacted_history_tail": compacted,
        "failure_state": failure_state,
    }


def _oracle(log):
    return {
        "run_id": log.run_id,
        "summary": log.projection.summary(),
        "history": log.history().render_projection(),
        "user_messages": list(log.history().user_texts()),
    }


def _started(log, call, *, path=None, before=None):
    effects = [] if path is None else [{"path": path, "before_state": before}]
    log.append("model_requested")
    log.append_assistant_turn(
        AssistantTurn(ModelAction.tool(call.name, call.args, call_id=call.call_id))
    )
    return log.append_tool_started(
        call.call_id,
        effect_scope="workspace",
        potential_effects=effects,
        operation={},
    )


def _tool_case(name, setup, expected):
    with tempfile.TemporaryDirectory(prefix=f"pico-{name}-") as directory:
        root = Path(directory)
        sessions, session, _store, log = _new_run(root)
        setup(root, log)
        runtime = _restore(root, sessions, session.id)
        recovered = runtime.tools.reconcile_interrupted()
        observed = None
        if recovered is not None:
            outcome, _entry = recovered
            observed = {
                "status": outcome.status,
                "execution_state": outcome.execution_state,
                "side_effect_state": outcome.side_effect_state,
                "affected_paths": list(outcome.affected_paths),
            }
        if observed != expected:
            raise AssertionError(f"{name}: expected {expected}, observed {observed}")
        if runtime.tools.reconcile_interrupted() is not None:
            raise AssertionError(f"{name}: reconciliation was not idempotent")


def _tool_cases():
    def before_started(root, _log):
        (root / "subject.txt").write_text("alpha\n")

    def unchanged(root, log):
        target = root / "subject.txt"
        target.write_text("alpha\n")
        _started(log, ToolCall("edit_file", {"path": "subject.txt"}, "edit"),
                path="subject.txt", before=file_revision(target))

    def published(root, log):
        target = root / "subject.txt"
        target.write_text("alpha\n")
        before = file_revision(target)
        _started(log, ToolCall("edit_file", {"path": "subject.txt"}, "edit"),
                path="subject.txt", before=before)
        target.write_text("beta\n")

    def deleted(root, log):
        target = root / "subject.txt"
        target.write_text("alpha\n")
        before = file_revision(target)
        _started(log, ToolCall("edit_file", {"path": "subject.txt"}, "edit"),
                path="subject.txt", before=before)
        target.unlink()

    def created(root, log):
        _started(log, ToolCall("write_file", {"path": "new.txt"}, "write"),
                path="new.txt", before="absent")
        (root / "new.txt").write_text("created\n")

    def shell_untracked(_root, log):
        _started(log, ToolCall("run_shell", {"command": "test"}, "shell"))

    def settled(root, log):
        target = root / "subject.txt"
        target.write_text("alpha\n")
        before = file_revision(target)
        call = ToolCall("edit_file", {"path": "subject.txt"}, "edit")
        _started(log, call, path="subject.txt", before=before)
        target.write_text("beta\n")
        after = file_revision(target)
        log.append_tool_result(
            ToolOutcome(
                call.call_id,
                call.name,
                "success",
                "completed",
                "changed",
                "edited",
                structured={
                    "path": "subject.txt",
                    "before_revision": before,
                    "after_revision": after,
                },
                affected_paths=("subject.txt",),
            )
        )

    none = None
    unchanged_outcome = {
        "status": "error",
        "execution_state": "failed",
        "side_effect_state": "none",
        "affected_paths": [],
    }
    partial = {
        "status": "partial_success",
        "execution_state": "failed",
        "side_effect_state": "partial",
        "affected_paths": ["subject.txt"],
    }
    return {
        "before_started": (before_started, none),
        "started_file_unchanged": (unchanged, unchanged_outcome),
        "published_before_result": (published, partial),
        "deleted_before_result": (deleted, partial),
        "created_before_result": (created, {**partial, "affected_paths": ["new.txt"]}),
        "shell_effect_untracked": (shell_untracked, {
            "status": "partial_success",
            "execution_state": "failed",
            "side_effect_state": "untracked",
            "affected_paths": [],
        }),
        "result_already_durable": (settled, none),
    }


def run():
    started = time.monotonic()
    results = []
    for name, setup in _task_cases().items():
        try:
            _task_case(name, setup)
            results.append({"category": "task", "scenario": name, "passed": True})
        except Exception as exc:  # noqa: BLE001 - report every scenario
            results.append({"category": "task", "scenario": name, "passed": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    for name, (setup, expected) in _tool_cases().items():
        try:
            _tool_case(name, setup, expected)
            results.append({"category": "tool", "scenario": name, "passed": True})
        except Exception as exc:  # noqa: BLE001 - report every scenario
            results.append({"category": "tool", "scenario": name, "passed": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    passed = sum(item["passed"] for item in results)
    return {
        "schema": "pico-recovery-eval-current",
        "source": _revision(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "passed": passed,
        "total": len(results),
        "results": results,
    }


def _markdown(report):
    lines = [
        "# Current Pico recovery evaluation",
        "",
        f"- Source: `{report['source']['commit']}`; dirty={str(report['source']['dirty']).lower()}",
        f"- Result: **{report['passed']}/{report['total']} passed**",
        f"- Duration: {report['duration_seconds']:.3f}s",
        "- Every row is a distinct recovery state; no repeated variants are used to inflate the count.",
        "",
        "| Category | Scenario | Result |",
        "|---|---|---|",
    ]
    for item in report["results"]:
        result = "passed" if item["passed"] else f"failed: {item.get('error', '')}"
        lines.append(f"| {item['category']} | `{item['scenario']}` | {result} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/metrics")
    args = parser.parse_args()
    report = run()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "recovery-current.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "recovery-current.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("passed", "total", "duration_seconds")}, sort_keys=True))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
