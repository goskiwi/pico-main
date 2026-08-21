"""Run creation, Run Log recovery, and terminal settlement."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .evidence import RunEvidence
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .run_log import RunLog
from .runtime_recovery import RESUME_READY
from .task_state import TaskState

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass
class AgentLoopState:
    user_message: str
    run_started_at: float
    task_state: TaskState
    run_log: RunLog
    prompt_snapshot: tuple[str, dict[str, Any]] | None = None
    provider_context_tokens: int | None = None
    overflow_recovery_attempted: bool = False
    invalid_output_count: int = 0
    execution_stop: str = ""


class RunLifecycle:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def initialize(self, user_message):
        runtime = self.runtime
        run_started_at = time.monotonic()
        runtime.run.begin_request()
        runtime.recovery.evaluate()
        task_state, run_log, resumed = self._restore_or_create_task(user_message)
        runtime.run.task_state = task_state
        runtime.run.run_log = run_log
        runtime.run.execution_context = self._root_execution(task_state)
        runtime.services.run_store.start_run(task_state)
        runtime.run.evidence = RunEvidence.from_events(run_log.events)

        reconciled = run_log.reconcile_interrupted(runtime)
        for _outcome, entry in reconciled:
            runtime.apply_run_event(entry)
        if resumed:
            runtime.apply_run_event(
                run_log.append_model_instruction(f"Resume request: {user_message}")
            )

        runtime.emit_event(
            task_state,
            "run_resumed" if resumed else "run_started",
            {
                "task_id": task_state.task_id,
                "workspace_root": str(runtime.workspace.root),
            },
        )
        runtime.model_client.reset_action_session()
        return AgentLoopState(
            user_message=user_message,
            run_started_at=run_started_at,
            task_state=task_state,
            run_log=run_log,
        )

    def _restore_or_create_task(self, user_message):
        runtime = self.runtime
        if runtime.recovery.state.get("status") == RESUME_READY:
            projection = runtime.recovery.state["projection"]
            events = runtime.recovery.state.pop("events")
            if not events:
                raise RuntimeError("resumable Run events are unavailable")
            first = events[0]
            run_log = RunLog(
                first.run_id,
                first.task_id,
                first.session_id,
                runtime.services.run_store,
                events,
            )
            task_state = TaskState.from_dict(projection.task_state())
            return task_state, run_log, True

        task_state = TaskState.create(
            run_id=runtime.new_run_id(),
            task_id=runtime.new_task_id(),
            user_request=user_message,
        )
        run_log = RunLog(
            task_state.run_id,
            task_state.task_id,
            runtime.session.data["id"],
            runtime.services.run_store,
        )
        run_log.append_user(user_message)
        runtime.session.set_active_run(task_state.run_id)
        return task_state, run_log, False

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

    def execution_stop(self):
        try:
            self.runtime.run.execution_context.check_active()
        except ExecutionDeadlineExceeded:
            return "deadline_exceeded"
        except ExecutionCancelled as exc:
            return str(exc) or "user_cancelled"
        return ""

    def finish_success(self, loop_state, final):
        runtime = self.runtime
        runtime.apply_run_event(
            loop_state.run_log.append_final(
                final,
                run_duration_ms=int(
                    (time.monotonic() - loop_state.run_started_at) * 1000
                ),
            )
        )
        try:
            runtime.session.set_active_run("")
        finally:
            execution = runtime.run.execution_context
            if execution is not None:
                execution.transition("completed")
                runtime.run.execution_context = None
        return final

    def finish_stopped(self, loop_state):
        if loop_state.execution_stop == "tool_execution_limit":
            loop_state.execution_stop = ""
        final, stop_reason = self._stopped_result(loop_state.execution_stop)
        self.runtime.apply_run_event(
            loop_state.run_log.append_stopped(
                final,
                stop_reason,
                run_duration_ms=int(
                    (time.monotonic() - loop_state.run_started_at) * 1000
                ),
            )
        )
        try:
            self.runtime.session.set_active_run("")
        finally:
            self._transition_stopped_execution(loop_state.execution_stop)
        return final

    @staticmethod
    def _stopped_result(stop):
        if stop == "invalid_output_limit":
            final = (
                "Stopped after too many invalid model outputs without a "
                "valid tool call or final answer."
            )
            stop_reason = "invalid_output_limit"
        elif stop:
            final = f"Stopped because execution was interrupted: {stop}."
            stop_reason = stop
        else:
            final = "Stopped after reaching the tool execution limit without a final answer."
            stop_reason = "tool_execution_limit"
        return final, stop_reason

    def _transition_stopped_execution(self, execution_stop):
        execution = self.runtime.run.execution_context
        if execution is None:
            return
        terminal_state = (
            "cancelled"
            if execution_stop and execution_stop != "deadline_exceeded"
            else "timed_out" if execution_stop else "completed"
        )
        execution.transition(terminal_state, stop_reason=execution_stop)
        self.runtime.run.execution_context = None
