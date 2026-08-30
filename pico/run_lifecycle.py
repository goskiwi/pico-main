"""Run creation, Run Log recovery, and terminal settlement."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .delivery import (
    build_final_diff_descriptor,
    build_stopped_final_diff_descriptor,
)
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .run_log import RunLog
from .run_projection import RunProjection
from .runtime_recovery import RESUME_NONE, RESUME_READY
from .task_state import TaskContract

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass
class AgentLoopState:
    user_message: str
    run_started_at: float
    prompt_snapshot: tuple[Any, dict[str, Any]] | None = None
    provider_context_tokens: int | None = None
    provider_overhead_tokens: int = 0
    overflow_recovery_attempted: bool = False
    invalid_output_count: int = 0
    completion_block_count: int = 0
    execution_stop: str = ""


class RunLifecycle:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def initialize(
        self,
        user_message,
        *,
        task_kind,
        requires_workspace_change,
        requires_verification,
    ):
        runtime = self.runtime
        run_started_at = time.monotonic()
        runtime.recovery.evaluate()
        projection, run_log, resumed = self._restore_or_create_run(
            user_message,
            task_kind=task_kind,
            requires_workspace_change=requires_workspace_change,
            requires_verification=requires_verification,
        )
        runtime.run.projection = projection
        runtime.run.run_log = run_log
        runtime.run.execution_context = self._root_execution()

        reconciled = run_log.reconcile_interrupted(runtime)
        for _outcome, entry in reconciled:
            runtime.apply_run_event(entry)
        if resumed:
            runtime.apply_run_event(
                run_log.append_model_instruction(f"Resume request: {user_message}")
            )

        runtime.emit_event(
            "run_resumed" if resumed else "run_started",
            {
                "task_id": runtime.run.projection.task_id,
                "workspace_root": str(runtime.workspace.root),
            },
        )
        runtime.model_client.reset_action_session()
        return AgentLoopState(
            user_message=user_message,
            run_started_at=run_started_at,
        )

    def _restore_or_create_run(
        self,
        user_message,
        *,
        task_kind,
        requires_workspace_change,
        requires_verification,
    ):
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
                runtime.dependencies.run_store,
                events,
            )
            if projection.task is None:
                raise RuntimeError("resumable Run task projection is unavailable")
            contract = projection.task.contract
            requested = (
                str(task_kind),
                bool(requires_workspace_change),
                bool(requires_verification),
            )
            persisted = (
                contract.task_kind,
                contract.requires_workspace_change,
                contract.requires_verification,
            )
            if requested != persisted:
                raise ValueError(
                    "resume task requirements do not match the persisted Run"
                )
            runtime.recovery.state = {
                "status": RESUME_NONE,
                "active_run_id": "",
                "projection": None,
                "events": (),
            }
            return projection, run_log, True

        run_id = runtime.new_run_id()
        task_id = runtime.new_task_id()
        contract = TaskContract(
            goal=user_message,
            task_kind=task_kind,
            requires_workspace_change=requires_workspace_change,
            requires_verification=requires_verification,
            allowed_write_paths=(
                None if task_kind == "read_only" else runtime.config.allowed_write_paths
            ),
        )
        run_log = RunLog(
            run_id,
            task_id,
            runtime.session.data["id"],
            runtime.dependencies.run_store,
        )
        first = run_log.append_user(contract)
        projection = RunProjection().apply_event(first)
        runtime.session.set_active_run(run_id)
        return projection, run_log, False

    def _root_execution(self):
        runtime = self.runtime
        token = (
            runtime.dependencies.parent_cancellation_token.child()
            if runtime.dependencies.parent_cancellation_token is not None
            else None
        )
        return ExecutionContext.root(
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
        final_diff = build_final_diff_descriptor(runtime)
        runtime.apply_run_event(
            runtime.run.run_log.append_final(
                final,
                final_diff,
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
                runtime.run.execution_context = None
        return final

    def finish_stopped(self, loop_state):
        final, stop_reason = self._stopped_result(loop_state.execution_stop)
        final_diff = build_stopped_final_diff_descriptor(self.runtime)
        self.runtime.apply_run_event(
            self.runtime.run.run_log.append_stopped(
                final,
                stop_reason,
                final_diff,
                run_duration_ms=int(
                    (time.monotonic() - loop_state.run_started_at) * 1000
                ),
            )
        )
        try:
            self.runtime.session.set_active_run("")
        finally:
            self.runtime.run.execution_context = None
        return final

    @staticmethod
    def _stopped_result(stop):
        if stop == "tool_execution_limit":
            final = "Stopped after reaching the tool execution limit without a final answer."
            stop_reason = "tool_execution_limit"
        elif stop == "invalid_output_limit":
            final = (
                "Stopped after too many invalid model outputs without a "
                "valid tool call or final answer."
            )
            stop_reason = "invalid_output_limit"
        elif stop == "completion_block_limit":
            final = "Stopped after repeated rejected completion attempts."
            stop_reason = "completion_block_limit"
        elif stop:
            final = f"Stopped because execution was interrupted: {stop}."
            stop_reason = stop
        else:
            raise ValueError("stopped Run requires a reason")
        return final, stop_reason
