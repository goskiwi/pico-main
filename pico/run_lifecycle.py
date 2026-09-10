"""Run creation, Run Log recovery, and terminal settlement."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .delivery import (
    build_final_diff,
    build_stopped_final_diff,
)
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .run_log import RunLog
from .run_projection import RunOutcome
from .runtime_state import ActiveRunState
from .task_state import TaskContract, WriteScope

if TYPE_CHECKING:
    from .prompt_builder import ModelPrompt
    from .runtime import Pico
    from .tool_runtime import ResolvedToolSurface


@dataclass
class AgentLoopState:
    user_message: str
    run_started_at: float
    prompt_snapshot: tuple[ModelPrompt, ResolvedToolSurface] | None = None
    provider_context_tokens: int | None = None
    overflow_recovery_attempted: bool = False
    last_request_input_tokens: int = 0
    invalid_output_count: int = 0
    completion_block_count: int = 0
    starting_model_request_count: int = 0


def _state_from_snapshot(runtime: Pico, run_log):
    projection = run_log.projection
    session_id = str(runtime.session.id)
    if not run_log.events:
        raise ValueError("active Run Log is missing or empty")
    if projection.run_id != run_log.run_id or projection.session_id != session_id:
        raise ValueError("active Run does not belong to this Session")
    return ActiveRunState(run_log=run_log)


def load_resumable_run(runtime: Pico):
    """Install the one validated unfinished Run named by this Session.

    A non-empty Session pointer is authoritative and therefore fails closed if
    its Run Log is absent, corrupt, or belongs to another Session.  Without a
    pointer, the Run Store may discover the latest orphaned unfinished Run.
    """

    session_id = str(runtime.session.id)
    if runtime.session.workspace_root != runtime.workspace.root:
        raise ValueError("session workspace does not match runtime workspace")

    pointed_run_id = str(runtime.session.active_run_id)
    if pointed_run_id:
        run_log = runtime.dependencies.run_store.load_run(pointed_run_id)
    else:
        run_log = runtime.dependencies.run_store.find_active_run(session_id)
    if run_log is None:
        return runtime.run

    projection = run_log.projection
    state = _state_from_snapshot(runtime, run_log)
    if projection.terminal:
        if pointed_run_id:
            runtime.session.set_active_run("")
        runtime.run = ActiveRunState()
        return runtime.run

    runtime.run = state
    if not pointed_run_id:
        runtime.session.set_active_run(run_log.run_id)
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
    if run.run_log is None or not run.run_log.events:
        return run
    run_id = run.run_log.run_id
    last_event = run.run_log.events[-1]
    projection_sequence = run.projection.last_sequence
    durable_sequence = runtime.dependencies.run_store.last_sequence(run_id)
    if (
        projection_sequence != last_event.sequence
        or projection_sequence != durable_sequence
    ):
        return reload_current_run(runtime)
    return run


class RunLifecycle:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def prepare_compaction(
        self,
        user_message,
        *,
        tool_surface,
        provider_context_tokens=None,
    ):
        inputs = self.runtime.prompt.prepare(user_message, tool_surface=tool_surface)
        plan, history = self.runtime.prompt.plan_compaction(
            inputs,
            provider_context_tokens=provider_context_tokens,
        )
        if plan is not None:
            self.runtime.run.run_log.append_compaction(*plan)
        return inputs, history

    def initialize(
        self,
        user_message,
    ):
        runtime = self.runtime
        run_started_at = time.monotonic()
        resumed = self._resume_or_create_run(
            user_message,
        )
        run_log = runtime.run.run_log
        if run_log is None:
            raise RuntimeError("Run initialization requires a Run Log")
        runtime.run.execution_context = self._root_execution()
        try:
            runtime.tools.reconcile_interrupted()
            if resumed:
                run_log.append_user_guidance(user_message)

            runtime.emit_event(
                "run_resumed" if resumed else "run_started",
                {
                    "workspace_root": str(runtime.workspace.root),
                },
            )
            runtime.model_client.reset_action_session()
        except BaseException:
            runtime.run.execution_context = None
            reload_current_run(runtime)
            raise
        return AgentLoopState(
            user_message=user_message,
            run_started_at=run_started_at,
            starting_model_request_count=runtime.run.metrics.model_request_count,
        )

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
        try:
            run_log.append_user(contract)
        except BaseException:
            runtime.run = ActiveRunState()
            load_resumable_run(runtime)
            raise
        runtime.run = ActiveRunState(run_log=run_log)
        runtime.session.set_active_run(run_id)
        return False

    def _task_contract(
        self,
        goal,
    ):
        config = self.runtime.config
        return TaskContract(
            goal=goal,
            write_scope=WriteScope.from_policy(config.mode, config.allowed_write_paths),
            verify_changes=(
                config.mode != "ask" and bool(config.verification_command)
            ),
        )

    def _root_execution(self):
        runtime = self.runtime
        return ExecutionContext.root(
            max_seconds=runtime.config.turn_timeout_seconds,
        )

    def execution_stop(self):
        try:
            self.runtime.run.execution_context.check_active()
        except ExecutionDeadlineExceeded:
            return "turn_timeout"
        except ExecutionCancelled as exc:
            return str(exc) or "user_cancelled"
        return ""

    def run_completion_verification(self, policy):
        """Execute and persist one completion verification attempt."""

        runtime = self.runtime
        sequence = runtime.run.evidence.last_workspace_mutation_sequence
        trace = runtime.dependencies.run_store.trace
        if trace is not None:
            trace.write("[Verification] checking…")
        current = runtime.run_verification(sequence, policy)
        if current is not None:
            runtime.emit_event("verification_result", current)
        # Preserve verifier facts before a stop requested while it was running.
        if runtime.run.execution_context is not None:
            runtime.run.execution_context.check_active()
        return current

    def finish_success(self, final, *, run_started_at) -> RunOutcome:
        runtime = self.runtime
        final_diff = build_final_diff(runtime)
        runtime.run.execution_context.check_active()
        runtime.run.run_log.append_final(
            final,
            final_diff,
            turn_duration_ms=int((time.monotonic() - run_started_at) * 1000),
        )
        outcome = RunOutcome(runtime.run.projection)
        try:
            runtime.session.set_active_run("")
        finally:
            execution = runtime.run.execution_context
            if execution is not None:
                runtime.run.execution_context = None
        return outcome

    def finish_stopped(self, stop_reason, *, run_started_at=None) -> RunOutcome:
        final, stop_reason = self._stopped_result(stop_reason)
        final_diff = build_stopped_final_diff(self.runtime)
        self.runtime.run.run_log.append_stopped(
            final,
            stop_reason,
            final_diff,
            turn_duration_ms=(
                0
                if run_started_at is None
                else int((time.monotonic() - run_started_at) * 1000)
            ),
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

    def reset_dormant(self) -> RunOutcome:
        """Settle one unfinished dormant Run through the normal terminal path."""

        try:
            self.runtime.tools.reconcile_interrupted()
            return self.finish_stopped("user_reset")
        except BaseException:
            reload_current_run(self.runtime)
            raise

    @staticmethod
    def _stopped_result(stop):
        if stop == "invalid_output_limit":
            final = (
                "Stopped after too many invalid model outputs without a "
                "valid tool call or final answer."
            )
            stop_reason = "invalid_output_limit"
        elif stop == "completion_block_limit":
            final = "Stopped after repeated rejected completion attempts."
            stop_reason = "completion_block_limit"
        elif stop == "agent_turn_limit":
            final = "Stopped after reaching the Agent turn limit."
            stop_reason = "agent_turn_limit"
        elif stop == "turn_timeout":
            final = "Stopped after reaching the active Turn timeout."
            stop_reason = "turn_timeout"
        elif stop:
            final = f"Stopped because execution was interrupted: {stop}."
            stop_reason = stop
        else:
            raise ValueError("stopped Run requires a reason")
        return final, stop_reason
