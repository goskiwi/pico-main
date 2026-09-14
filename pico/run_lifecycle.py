"""Run creation, Run Log recovery, and terminal settlement."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .run_log import RunLog
from .run_projection import RunOutcome
from .runtime_state import ActiveRunState
from .task_state import TaskContract, WriteScope

if TYPE_CHECKING:
    from .runtime import Pico


def _state_from_snapshot(runtime: Pico, run_log):
    projection = run_log.projection
    session_id = str(runtime.session.id)
    if run_log.projection.last_sequence < 1:
        raise ValueError("active Run Log is missing or empty")
    if projection.run_id != run_log.run_id or projection.session_id != session_id:
        raise ValueError("active Run does not belong to this Session")
    return ActiveRunState(run_log=run_log)


def load_resumable_run(runtime: Pico):
    """Install the one validated unfinished Run named by this Session."""

    if runtime.session.workspace_root != runtime.workspace.root:
        raise ValueError("session workspace does not match runtime workspace")

    pointed_run_id = str(runtime.session.active_run_id)
    if not pointed_run_id:
        return runtime.run
    if not runtime.dependencies.run_store.has_events(pointed_run_id):
        runtime.session.set_active_run("")
        return runtime.run
    run_log = runtime.dependencies.run_store.load_run(pointed_run_id)

    projection = run_log.projection
    state = _state_from_snapshot(runtime, run_log)
    if projection.terminal:
        runtime.session.set_active_run("")
        runtime.run = ActiveRunState()
        return runtime.run

    runtime.run = state
    return runtime.run


def reload_current_run(runtime: Pico):
    """Replace possibly ambiguous in-memory state with its durable snapshot."""

    if runtime.run.run_log is None:
        return load_resumable_run(runtime)
    run_id = runtime.run.run_log.run_id
    run_log = runtime.dependencies.run_store.load_run(run_id)
    projection = run_log.projection
    runtime.run = _state_from_snapshot(runtime, run_log)
    pointed_run_id = str(runtime.session.active_run_id)
    expected_pointer = "" if projection.terminal else run_id
    if pointed_run_id != expected_pointer:
        runtime.session.set_active_run(expected_pointer)
    return runtime.run


def _reload_if_snapshot_is_stale(runtime: Pico):
    run = runtime.run
    if run.run_log is None or run.projection.last_sequence < 1:
        return run
    run_id = run.run_log.run_id
    projection_sequence = run.projection.last_sequence
    durable_sequence = runtime.dependencies.run_store.last_sequence(run_id)
    if projection_sequence != durable_sequence:
        return reload_current_run(runtime)
    return run


class RunLifecycle:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def initialize(
        self,
        user_message,
    ):
        runtime = self.runtime
        attempt_started_at = time.monotonic()
        resumed = self._resume_or_create_run(
            user_message,
        )
        run_log = runtime.run.run_log
        if run_log is None:
            raise RuntimeError("Run initialization requires a Run Log")
        runtime.run.execution_context = self._root_execution()
        try:
            runtime.tools.reconcile_interrupted()
            runtime.emit_event(
                "run_resumed" if resumed else "run_started",
                {
                    "workspace_root": str(runtime.workspace.root),
                },
            )
            if resumed:
                run_log.append_user_guidance(runtime.redact_text(user_message))
            runtime.model_client.reset_action_session()
            runtime.dependencies.run_store.checkpoint_if_due(run_log)
        except BaseException:
            runtime.run.execution_context = None
            reload_current_run(runtime)
            raise
        return attempt_started_at

    def _resume_or_create_run(
        self,
        user_message,
    ):
        runtime = self.runtime
        _reload_if_snapshot_is_stale(runtime)
        if runtime.run.resumable:
            if runtime.session.active_run_id != runtime.run.projection.run_id:
                runtime.session.set_active_run(runtime.run.projection.run_id)
            return True

        if runtime.run.run_log is not None:
            if runtime.run.projection.contract is None:
                raise RuntimeError("Run Log has no TaskContract")
            if not runtime.run.projection.terminal:
                raise RuntimeError("unfinished Run is not dormant and cannot be resumed")

        run_id = runtime.new_run_id()
        contract = self._task_contract(
            user_message,
        )
        run_log = RunLog(
            run_id,
            runtime.session.id,
            runtime.dependencies.run_store,
        )
        runtime.session.set_active_run(run_id)
        runtime.run = ActiveRunState(run_log=run_log)
        try:
            run_log.append_user(contract)
        except BaseException:
            runtime.run = ActiveRunState()
            load_resumable_run(runtime)
            raise
        return False

    def _task_contract(
        self,
        goal,
    ):
        config = self.runtime.config
        configured_tools = config.allowed_tools
        return TaskContract(
            goal=self.runtime.redact_text(goal),
            mode=config.mode,
            allowed_tools=tuple(
                name
                for name in self.runtime.tools.registry
                if configured_tools is None or name in configured_tools
            ),
            write_scope=WriteScope.from_policy(config.mode, config.allowed_write_paths),
        )

    def _root_execution(self):
        runtime = self.runtime
        return ExecutionContext.root(
            max_seconds=runtime.config.attempt_timeout_seconds,
        )

    def execution_stop(self):
        try:
            self.runtime.run.execution_context.check_active()
        except ExecutionDeadlineExceeded:
            return "attempt_timeout"
        except ExecutionCancelled as exc:
            return str(exc) or "user_cancelled"
        return ""

    def finish_success(self, turn, *, attempt_started_at) -> RunOutcome:
        runtime = self.runtime
        runtime.run.execution_context.check_active()
        safe_turn = runtime.redact_turn(turn)
        runtime.run.run_log.append_assistant_turn(
            safe_turn,
            attempt_duration_ms=int(
                (time.monotonic() - attempt_started_at) * 1000
            ),
        )
        return self._finish_run()

    def finish_stopped(self, stop_reason, *, attempt_started_at=None) -> RunOutcome:
        final, stop_reason = self._stopped_result(stop_reason)
        self.runtime.run.run_log.append_stopped(
            final,
            stop_reason,
            attempt_duration_ms=(
                0
                if attempt_started_at is None
                else int((time.monotonic() - attempt_started_at) * 1000)
            ),
        )
        runtime = self.runtime
        outcome = self._finish_run()
        if stop_reason == "user_reset":
            runtime.run = ActiveRunState()
            runtime.model_client.reset_action_session()
        return outcome

    def _finish_run(self):
        runtime = self.runtime
        outcome = RunOutcome(runtime.run.projection)
        try:
            runtime.session.set_active_run("")
        finally:
            runtime.run.execution_context = None
        return outcome

    def reset_dormant(self) -> RunOutcome:
        """Settle one unfinished dormant Run through the normal terminal path."""

        try:
            self.runtime.tools.reconcile_interrupted()
            if self.runtime.run.projection.phase == "requesting_model":
                self.runtime.run.run_log.append_model_failure(
                    "interrupted",
                    "user_reset",
                    "user_reset",
                    {},
                )
            return self.finish_stopped("user_reset")
        except BaseException:
            reload_current_run(self.runtime)
            raise

    @staticmethod
    def _stopped_result(stop):
        if stop == "model_failure_limit":
            final = (
                "Stopped after too many invalid model outputs without a "
                "valid tool call or final answer."
            )
            stop_reason = "model_failure_limit"
        elif stop == "provider_failure":
            final = "Stopped after the Provider request failed after bounded retries."
            stop_reason = "provider_failure"
        elif stop == "repeated_failure":
            final = "Stopped after the same failure repeated without relevant progress."
            stop_reason = "repeated_failure"
        elif stop == "model_request_limit":
            final = "Stopped after reaching the model request limit for this attempt."
            stop_reason = "model_request_limit"
        elif stop == "attempt_timeout":
            final = "Stopped after reaching the active execution attempt timeout."
            stop_reason = "attempt_timeout"
        elif stop:
            final = f"Stopped because execution was interrupted: {stop}."
            stop_reason = stop
        else:
            raise ValueError("stopped Run requires a reason")
        return final, stop_reason
