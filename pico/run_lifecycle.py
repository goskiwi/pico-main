"""Run creation, Journal recovery, and terminal settlement."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .completion import CompletionGate
from .evidence import EvidenceLedger
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .run_journal import RunJournal
from .runtime_recovery import RESUME_READY
from .task_state import TaskState

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass
class LoopFrame:
    user_message: str
    run_started_at: float
    task_state: TaskState
    journal: RunJournal
    completion_gate: CompletionGate
    context_generation: int
    prompt_snapshot: tuple[str, dict[str, Any]] | None = None
    tool_steps: int = 0
    attempts: int = 0
    malformed_retries: int = 0
    execution_stop: str = ""


class RunLifecycle:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def initialize(self, user_message):
        runtime = self.runtime
        run_started_at = time.monotonic()
        runtime.run.begin_request()
        if runtime.run.task_state is not None:
            runtime.recovery.evaluate()
        task_state, journal, resumed = self._restore_or_create_task(user_message)
        runtime.session.memory.set_goal(task_state.user_request)
        runtime.session.save()
        runtime.run.task_state = task_state
        runtime.run.journal = journal
        runtime.run.execution = self._root_execution(task_state)
        runtime.run.run_dir = runtime.services.run_store.start_run(task_state)
        runtime.run.evidence = EvidenceLedger.from_entries(journal.entries)

        reconciled = journal.reconcile_interrupted(runtime)
        for outcome, entry in reconciled:
            runtime.run.evidence.apply_entry(entry)
            projection = runtime.recovery.state.get("projection")
            if projection is not None:
                projection.apply(entry)
            if outcome.execution_state != "not_started":
                task_state.record_tool(outcome.tool_name)
        completion_gate = self._completion_gate(journal)
        if resumed:
            journal.append_guidance(f"Resume request: {user_message}")

        runtime.emit_event(
            task_state,
            "run_resumed" if resumed else "run_started",
            {
                "task_id": task_state.task_id,
                "workspace_root": str(runtime.workspace.root),
            },
        )
        runtime.model_client.reset_action_session()
        return LoopFrame(
            user_message=user_message,
            run_started_at=run_started_at,
            task_state=task_state,
            journal=journal,
            completion_gate=completion_gate,
            context_generation=journal.generation,
            tool_steps=task_state.tool_steps,
            attempts=task_state.attempts,
        )

    def _restore_or_create_task(self, user_message):
        runtime = self.runtime
        if runtime.recovery.state.get("status") == RESUME_READY:
            projection = runtime.recovery.state["projection"]
            entries = runtime.recovery.state.pop("entries")
            if not entries:
                raise RuntimeError("resumable Journal entries are unavailable")
            first = entries[0]
            journal = RunJournal(
                first.run_id,
                first.task_id,
                first.session_id,
                runtime.services.run_store,
                entries,
            )
            task_state = TaskState.from_dict(projection.task_state())
            return task_state, journal, True

        task_state = TaskState.create(
            run_id=runtime.new_run_id(),
            task_id=runtime.new_task_id(),
            user_request=user_message,
        )
        runtime.services.run_store.start_run(task_state)
        runtime.session.data["active_run_id"] = task_state.run_id
        runtime.session.save()
        journal = RunJournal(
            task_state.run_id,
            task_state.task_id,
            runtime.session.data["id"],
            runtime.services.run_store,
        )
        journal.append_user(user_message)
        return task_state, journal, False

    def _root_execution(self, task_state):
        runtime = self.runtime
        token = (
            runtime.services.parent_cancellation_token.child()
            if runtime.services.parent_cancellation_token is not None
            else None
        )
        return ExecutionContext.root(
            run_id=task_state.run_id,
            task_id=task_state.task_id,
            owner="agent_loop",
            max_seconds=runtime.config.run_timeout_seconds,
            token=token,
        )

    @staticmethod
    def _completion_gate(journal):
        gate = CompletionGate()
        for entry in journal.entries:
            if entry.kind != "tool_result":
                continue
            outcome = dict(entry.payload.get("outcome", {}) or {})
            if outcome.get("status") == "partial_success" or outcome.get(
                "side_effect_state"
            ) == "unknown":
                paths = outcome.get("affected_paths", []) or [
                    f"operation:{entry.call_id}"
                ]
                gate.restore_partial_paths(paths)
        return gate

    def execution_stop(self):
        try:
            self.runtime.run.execution.check_active()
        except ExecutionDeadlineExceeded:
            return "deadline_exceeded"
        except ExecutionCancelled as exc:
            return str(exc) or "user_cancelled"
        return ""

    def finish_success(self, frame, final):
        runtime = self.runtime
        frame.journal.append_final(
            final,
            run_duration_ms=int((time.monotonic() - frame.run_started_at) * 1000),
        )
        frame.task_state.finish_success(final)
        runtime.session.data["active_run_id"] = ""
        runtime.session.save()
        runtime.run.execution.transition("completed")
        runtime.run.execution = None
        return final

    def finish_stopped(self, frame):
        if frame.execution_stop == "step_limit_reached":
            frame.execution_stop = ""
        final = self._apply_stop_state(frame)
        frame.journal.append_stopped(
            final,
            frame.task_state.stop_reason,
            run_duration_ms=int((time.monotonic() - frame.run_started_at) * 1000),
        )
        self.runtime.session.data["active_run_id"] = ""
        self.runtime.session.save()
        self._transition_stopped_execution(frame.execution_stop)
        return final

    @staticmethod
    def _apply_stop_state(frame):
        stop = frame.execution_stop
        if stop.startswith("policy:"):
            reason = stop.removeprefix("policy:")
            final = f"Stopped by runtime policy: {reason}."
            frame.task_state.stop("policy_stop", final_answer=final)
        elif stop == "malformed_model_retry_limit":
            final = (
                "Stopped after too many malformed model responses without a "
                "valid tool call or final answer."
            )
            frame.task_state.stop_retry_limit(final)
        elif stop:
            final = f"Stopped because execution was interrupted: {stop}."
            frame.task_state.stop(stop, final_answer=final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            frame.task_state.stop_step_limit(final)
        return final

    def _transition_stopped_execution(self, execution_stop):
        execution = self.runtime.run.execution
        if execution is None:
            return
        terminal_state = (
            "cancelled"
            if execution_stop and execution_stop != "deadline_exceeded"
            else "timed_out" if execution_stop else "completed"
        )
        execution.transition(terminal_state, stop_reason=execution_stop)
        self.runtime.run.execution = None
