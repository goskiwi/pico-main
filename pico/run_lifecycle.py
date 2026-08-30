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
from .run_projection import RunOutcome, RunProjection
from .runtime_state import ActiveRunState
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


def _state_from_snapshot(runtime: Pico, run_id, events, projection):
    session_id = str(runtime.session.data["id"])
    if not events:
        raise ValueError("active Run Log is missing or empty")
    if projection.run_id != run_id or projection.session_id != session_id:
        raise ValueError("active Run does not belong to this Session")
    first = events[0]
    run_log = RunLog(
        first.run_id,
        first.task_id,
        first.session_id,
        runtime.dependencies.run_store,
        events,
    )
    return ActiveRunState(projection=projection, run_log=run_log)


def load_resumable_run(runtime: Pico):
    """Install the one validated unfinished Run named by this Session.

    A non-empty Session pointer is authoritative and therefore fails closed if
    its Run Log is absent, corrupt, or belongs to another Session.  Without a
    pointer, the Run Store may discover the latest orphaned unfinished Run.
    """

    session_id = str(runtime.session.data["id"])
    if runtime.session.data.get("workspace_root") != str(runtime.workspace.root):
        raise ValueError("session workspace does not match runtime workspace")

    pointed_run_id = str(runtime.session.data.get("active_run_id", ""))
    if pointed_run_id:
        run_id = pointed_run_id
        events, projection = runtime.dependencies.run_store.load_run(run_id)
    else:
        run_id, events, projection = runtime.dependencies.run_store.find_active_run(
            session_id
        )
    if (
        not run_id
        and runtime.run.reload_required
        and runtime.run.run_log is not None
    ):
        candidate_run_id = runtime.run.run_log.run_id
        events, projection = runtime.dependencies.run_store.load_run(
            candidate_run_id
        )
        if events:
            run_id = candidate_run_id
        else:
            runtime.run = ActiveRunState()
    if not run_id:
        return runtime.run

    state = _state_from_snapshot(runtime, run_id, events, projection)
    if projection.terminal:
        if pointed_run_id:
            runtime.session.set_active_run("")
        runtime.run = ActiveRunState()
        return runtime.run

    runtime.run = state
    if not pointed_run_id:
        runtime.session.set_active_run(run_id)
    return runtime.run


def reload_current_run(runtime: Pico):
    """Replace possibly ambiguous in-memory state with its durable snapshot."""

    run_id = str(runtime.run.projection.run_id)
    if not run_id:
        return load_resumable_run(runtime)
    events, projection = runtime.dependencies.run_store.load_run(run_id)
    runtime.run = _state_from_snapshot(runtime, run_id, events, projection)
    pointed_run_id = str(runtime.session.data.get("active_run_id", ""))
    expected_pointer = "" if projection.terminal else run_id
    if pointed_run_id != expected_pointer:
        runtime.session.set_active_run(expected_pointer)
    return runtime.run


def _reload_if_snapshot_is_stale(runtime: Pico):
    run = runtime.run
    run_id = str(run.projection.run_id)
    if run.reload_required:
        return reload_current_run(runtime)
    if not run_id or run.run_log is None or not run.run_log.events:
        return run
    last_event = run.run_log.events[-1]
    projection_cursor = run.projection.last_cursor
    durable_cursor = runtime.dependencies.run_store.cursor(run_id)
    if (
        projection_cursor.sequence != last_event.sequence
        or projection_cursor.event_id != last_event.event_id
        or projection_cursor != durable_cursor
    ):
        return reload_current_run(runtime)
    return run


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
        resumed = self._resume_or_create_run(
            user_message,
            task_kind=task_kind,
            requires_workspace_change=requires_workspace_change,
            requires_verification=requires_verification,
        )
        run_log = runtime.run.run_log
        if run_log is None:
            raise RuntimeError("Run initialization requires a Run Log")
        runtime.run.execution_context = self._root_execution()
        try:
            reconciled = run_log.reconcile_interrupted(runtime)
            for _outcome, entry in reconciled:
                runtime.apply_run_event(entry)

            runtime.emit_event(
                "run_resumed" if resumed else "run_started",
                {
                    "task_id": runtime.run.projection.task_id,
                    "workspace_root": str(runtime.workspace.root),
                },
            )
            runtime.model_client.reset_action_session()
        except BaseException:
            runtime.run.reload_required = True
            runtime.run.execution_context = None
            reload_current_run(runtime)
            raise
        return AgentLoopState(
            user_message=user_message,
            run_started_at=run_started_at,
        )

    def _resume_or_create_run(
        self,
        user_message,
        *,
        task_kind,
        requires_workspace_change,
        requires_verification,
    ):
        runtime = self.runtime
        _reload_if_snapshot_is_stale(runtime)
        if runtime.run.resumable:
            persisted = runtime.run.task.contract
            requested = TaskContract(
                goal=persisted.goal,
                task_kind=task_kind,
                requires_workspace_change=requires_workspace_change,
                requires_verification=requires_verification,
                allowed_write_paths=persisted.allowed_write_paths,
            )
            if requested != persisted:
                raise ValueError(
                    "resume task requirements do not match the persisted Run"
                )
            if (
                runtime.session.data.get("active_run_id")
                != runtime.run.projection.run_id
            ):
                runtime.session.set_active_run(runtime.run.projection.run_id)
            return True

        if runtime.run.task is not None and not runtime.run.projection.terminal:
            raise RuntimeError("unfinished Run is not dormant and cannot be resumed")
        if runtime.run.run_log is not None and runtime.run.task is None:
            raise RuntimeError("Run state contains a Run Log without a TaskContract")

        run_id = runtime.new_run_id()
        task_id = runtime.new_task_id()
        contract = self._task_contract(
            user_message,
            task_kind=task_kind,
            requires_workspace_change=requires_workspace_change,
            requires_verification=requires_verification,
        )
        run_log = RunLog(
            run_id,
            task_id,
            runtime.session.data["id"],
            runtime.dependencies.run_store,
        )
        try:
            first = run_log.append_user(contract)
            projection = RunProjection().apply_event(first)
        except BaseException:
            runtime.run = ActiveRunState(run_log=run_log, reload_required=True)
            load_resumable_run(runtime)
            raise
        runtime.run = ActiveRunState(projection=projection, run_log=run_log)
        runtime.session.set_active_run(run_id)
        return False

    def _task_contract(
        self,
        goal,
        *,
        task_kind,
        requires_workspace_change,
        requires_verification,
    ):
        return TaskContract(
            goal=goal,
            task_kind=task_kind,
            requires_workspace_change=requires_workspace_change,
            requires_verification=requires_verification,
            allowed_write_paths=(
                None
                if task_kind == "read_only"
                else self.runtime.config.allowed_write_paths
            ),
        )

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

    def finish_success(self, loop_state, final) -> RunOutcome:
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
        outcome = RunOutcome(runtime.run.projection)
        try:
            runtime.session.set_active_run("")
        finally:
            execution = runtime.run.execution_context
            if execution is not None:
                runtime.run.execution_context = None
        return outcome

    def finish_stopped(self, loop_state) -> RunOutcome:
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
        runtime = self.runtime
        outcome = RunOutcome(runtime.run.projection)
        try:
            runtime.session.set_active_run("")
        finally:
            runtime.run.execution_context = None
        if stop_reason == "user_reset":
            runtime.run = ActiveRunState()
            runtime.model_client.reset_action_session()
        return outcome

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
