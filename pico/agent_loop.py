"""Agent control loop extracted from the runtime facade."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .checkpoint import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
    task_state_from_checkpoint,
)
from .completion import CompletionGate
from .context_ledger import ContextLedger
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .hooks import AfterToolContext, TurnContext
from .task_state import TaskState
from .verification import changed_python_syntax_issues
from .workspace import clip


@dataclass
class LoopFrame:
    """Ephemeral loop variables; durable facts remain Runtime-owned projections."""

    user_message: str
    run_started_at: float
    task_state: TaskState
    ledger: ContextLedger
    completion_gate: CompletionGate
    context_generation: int
    prompt_snapshot: tuple[str, dict[str, Any]] | None = None
    tool_steps: int = 0
    attempts: int = 0
    malformed_retries: int = 0
    execution_stop: str = ""


@dataclass(frozen=True)
class ModelTurn:
    action: Any
    prompt_metadata: dict[str, Any]
    reset_provider_session: bool
    provider_input_tokens: int | None


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message):
        frame = self._initialize_run(user_message)
        while True:
            frame.execution_stop = self._execution_stop()
            if frame.execution_stop:
                break

            turn = self._next_model_turn(frame)
            if turn.action.kind == "tool":
                frame.execution_stop = self._handle_tool_action(frame, turn)
            elif turn.action.kind == "retry":
                frame.execution_stop = self._handle_retry_action(frame, turn)
            else:
                final = self._handle_final_action(frame, turn)
                if final is not None:
                    return self._finish_success(frame, final)
            if frame.execution_stop:
                break
        return self._finish_stopped(frame)

    def _initialize_run(self, user_message):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.memory.set_goal(user_message)
        agent._task_memory_selection = None
        agent.evidence_ledger = type(agent.evidence_ledger)()

        checkpoint, can_resume, task_state, ledger = self._restore_or_create_task(
            user_message
        )
        task_state.resume_status = agent.resume_state.get(
            "status", CHECKPOINT_NONE_STATUS
        )
        agent.current_task_state = task_state
        agent.current_execution = self._root_execution(task_state)
        agent.current_run_dir = agent.run_store.start_run(task_state)
        if ledger is None:
            ledger = ContextLedger(task_state.run_id, agent.run_store)
            ledger.append_user(user_message)
        agent.context_ledger = ledger

        completion_gate = self._completion_gate(checkpoint, can_resume, ledger)
        self._emit_run_started(task_state, user_message, can_resume)
        self._reconcile_interrupted_outcomes(task_state, ledger, completion_gate)
        agent.model_client.reset_action_session()
        return LoopFrame(
            user_message=user_message,
            run_started_at=run_started_at,
            task_state=task_state,
            ledger=ledger,
            completion_gate=completion_gate,
            context_generation=ledger.generation,
            tool_steps=task_state.tool_steps,
            attempts=task_state.attempts,
        )

    def _restore_or_create_task(self, user_message):
        agent = self.agent
        checkpoint = agent.current_checkpoint()
        saved_task = (
            task_state_from_checkpoint(agent, checkpoint)
            if checkpoint
            and agent.resume_state.get("status") == CHECKPOINT_FULL_VALID_STATUS
            else {}
        )
        can_resume = bool(
            agent.resume_state.get("status") == CHECKPOINT_FULL_VALID_STATUS
            and saved_task.get("status") == "running"
            and not saved_task.get("stop_reason")
            and (checkpoint or {}).get("context_run_id")
        )
        if not can_resume:
            task_state = TaskState.create(
                run_id=agent.new_run_id(),
                task_id=agent.new_task_id(),
                user_request=user_message,
            )
            return checkpoint, False, task_state, None

        prior_events = agent.run_store.read_events(checkpoint["context_run_id"])
        task_state = TaskState.from_dict(saved_task)
        ledger = ContextLedger.restore(checkpoint["context_run_id"], agent.run_store)
        ledger.append_guidance(f"Resume request: {user_message}")
        agent.evidence_ledger = type(agent.evidence_ledger).from_events(prior_events)
        return checkpoint, True, task_state, ledger

    def _root_execution(self, task_state):
        agent = self.agent
        token = (
            agent.parent_cancellation_token.child()
            if agent.parent_cancellation_token is not None
            else None
        )
        return ExecutionContext.root(
            run_id=task_state.run_id,
            task_id=task_state.task_id,
            owner="agent_loop",
            max_seconds=agent.run_timeout_seconds,
            token=token,
        )

    @staticmethod
    def _completion_gate(checkpoint, can_resume, ledger):
        gate = CompletionGate()
        gate.restore_partial_paths(
            (checkpoint or {}).get("pending_partial_paths", []) if can_resume else []
        )
        gate.restore_partial_paths(
            path
            for entry in ledger.active_entries()
            if entry.kind == "tool_result"
            and (
                entry.outcome_status == "partial_success"
                or entry.side_effect_state == "unknown"
            )
            for path in (entry.affected_paths or (f"operation:{entry.call_id}",))
        )
        return gate

    def _emit_run_started(self, task_state, user_message, can_resume):
        self.agent.emit_event(
            task_state,
            "run_resumed" if can_resume else "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

    def _reconcile_interrupted_outcomes(self, task_state, ledger, completion_gate):
        agent = self.agent
        for outcome in ledger.reconciled_outcomes:
            task_state.record_tool(outcome.tool_name)
            completion_gate.observe(outcome)
            event = agent.emit_event(
                task_state,
                "operation_finished",
                {
                    "tool_call_id": outcome.tool_call_id,
                    "tool_name": outcome.tool_name,
                    "content_workspace_fingerprint": (
                        agent.content_workspace_fingerprint()
                    ),
                    "recovered_from_interruption": True,
                    "outcome": outcome.to_dict(),
                },
                correlation_id=outcome.tool_call_id,
            )
            agent.evidence_ledger.apply_event(event)
        if ledger.reconciled_outcomes:
            agent.run_store.write_task_state(task_state)

    def _execution_stop(self):
        try:
            self.agent.current_execution.check_active()
        except ExecutionDeadlineExceeded:
            return "deadline_exceeded"
        except ExecutionCancelled as exc:
            return str(exc) or "user_cancelled"
        return ""

    def _next_model_turn(self, frame):
        agent = self.agent
        frame.attempts += 1
        frame.task_state.record_attempt()
        agent.run_store.write_task_state(frame.task_state)
        prompt, prompt_metadata = self._prepare_prompt(frame)
        agent.emit_event(
            frame.task_state,
            "model_requested",
            {
                "attempts": frame.task_state.attempts,
                "tool_steps": frame.task_state.tool_steps,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
            },
        )
        action, completion_metadata = self._request_action(
            frame, prompt, prompt_metadata
        )
        provider_input_tokens = completion_metadata.get("input_tokens")
        reset_provider_session = bool(
            isinstance(provider_input_tokens, int)
            and provider_input_tokens + agent.max_new_tokens
            >= agent.provider_context_limit_tokens
        )
        return ModelTurn(
            action=action,
            prompt_metadata=prompt_metadata,
            reset_provider_session=reset_provider_session,
            provider_input_tokens=provider_input_tokens,
        )

    def _prepare_prompt(self, frame):
        agent = self.agent
        prompt_started_at = time.monotonic()
        prompt_reused = frame.prompt_snapshot is not None
        if frame.prompt_snapshot is None:
            prompt, prompt_metadata = agent._build_prompt_and_metadata(
                frame.user_message
            )
            frame.prompt_snapshot = (prompt, dict(prompt_metadata))
        else:
            prompt, original_metadata = frame.prompt_snapshot
            prompt_metadata = dict(original_metadata)
        prompt_metadata["prompt_reused"] = prompt_reused
        prompt_metadata["provider_session_active"] = prompt_reused
        self._record_prompt_projection(frame, prompt_metadata, prompt_reused)
        agent.emit_event(
            frame.task_state,
            "prompt_built",
            {
                "prompt_metadata": prompt_metadata,
                "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
            },
        )
        self._checkpoint_prompt_changes(frame, prompt_metadata, prompt_reused)
        return prompt, prompt_metadata

    def _record_prompt_projection(self, frame, prompt_metadata, prompt_reused):
        if prompt_reused:
            return
        agent = self.agent
        next_generation = int(
            prompt_metadata.get("ledger_generation", frame.ledger.generation)
        )
        if next_generation > frame.context_generation:
            frame.context_generation = next_generation
            agent.emit_event(
                frame.task_state,
                "context_folded",
                {"generation": frame.context_generation},
            )
        memory_audit = dict(prompt_metadata.get("memory_retrieval", {}) or {})
        if memory_audit.get("available_count") or memory_audit.get(
            "selected_filenames"
        ):
            agent.emit_event(frame.task_state, "memory_selection", memory_audit)

    def _checkpoint_prompt_changes(self, frame, prompt_metadata, prompt_reused):
        if prompt_reused:
            return
        resume_status = prompt_metadata.get("resume_status")
        if resume_status == CHECKPOINT_PARTIAL_STALE_STATUS:
            self._create_checkpoint(frame, "freshness_mismatch")
        elif resume_status == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
            self.agent.emit_event(
                frame.task_state,
                "runtime_identity_mismatch",
                {
                    "fields": list(
                        prompt_metadata.get(
                            "runtime_identity_mismatch_fields", []
                        )
                    ),
                },
            )
            self._create_checkpoint(frame, "workspace_mismatch")
        if prompt_metadata.get("budget_reductions"):
            self._create_checkpoint(frame, "context_reduction")

    def _create_checkpoint(self, frame, trigger):
        agent = self.agent
        checkpoint = agent.create_checkpoint(
            frame.task_state,
            frame.user_message,
            trigger=trigger,
        )
        agent.run_store.write_task_state(frame.task_state)
        agent.emit_event(
            frame.task_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": trigger},
        )
        return checkpoint

    def _request_action(self, frame, prompt, prompt_metadata):
        agent = self.agent
        prompt_cache_key = (
            prompt_metadata.get("prompt_cache_key")
            if getattr(agent.model_client, "supports_prompt_cache", False)
            else None
        )
        action_tools = (
            [tool for tool in agent.action_tools if tool["name"] == "submit_final"]
            if agent.max_steps is not None and frame.tool_steps >= agent.max_steps
            else agent.action_tools
        )
        model_started_at = time.monotonic()
        action = agent.model_client.complete_action(
            prompt,
            agent.max_new_tokens,
            action_tools=action_tools,
            prompt_cache_key=prompt_cache_key,
            request_timeout=agent.current_execution.bounded_timeout(),
        )
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        agent.emit_event(
            frame.task_state,
            "model_parsed",
            {
                "kind": action.kind,
                "tool_call_id": (
                    action.tool_call.call_id if action.tool_call else ""
                ),
                "completion_metadata": completion_metadata,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
            },
        )
        return action, completion_metadata

    def _continue_provider(self, frame, turn, feedback, tool_call_id=""):
        agent = self.agent
        if turn.reset_provider_session:
            agent.model_client.reset_action_session()
            frame.prompt_snapshot = None
            agent.emit_event(
                frame.task_state,
                "provider_session_reset",
                {
                    "reason": "input_threshold",
                    "input_tokens": turn.provider_input_tokens,
                    "tool_call_id": tool_call_id,
                },
                correlation_id=tool_call_id,
            )
            return
        agent.model_client.record_action_result(turn.action, feedback)

    def _handle_tool_action(self, frame, turn):
        agent = self.agent
        if agent.max_steps is not None and frame.tool_steps >= agent.max_steps:
            return "step_limit_reached"
        frame.malformed_retries = 0
        call = turn.action.tool_call
        frame.ledger.append_tool_call(call)
        outcome = agent.run_tool(call)
        if outcome.execution_state != "not_started":
            frame.tool_steps += 1
            frame.task_state.record_tool(call.name)
        frame.completion_gate.observe(outcome)
        frame.ledger.append_tool_result(outcome)

        guidance, policy_stop, reason = self._tool_policy(frame, call, outcome)
        provider_result = outcome.content
        if guidance:
            provider_result += "\n\nRuntime guidance: " + guidance
        self._continue_provider(
            frame,
            turn,
            provider_result,
            tool_call_id=call.call_id,
        )
        self._create_checkpoint(frame, "tool_executed")
        if policy_stop:
            return "policy:" + (reason or "runtime policy requested stop")
        return ""

    def _tool_policy(self, frame, call, outcome):
        agent = self.agent
        hook_decision = agent.hooks.after_tool_result(
            AfterToolContext(
                outcome=outcome,
                tool_steps=frame.tool_steps,
                run_id=frame.task_state.run_id,
                task_id=frame.task_state.task_id,
            )
        )
        turn_decision = agent.hooks.should_stop_after_turn(
            TurnContext(
                action_kind="tool",
                tool_steps=frame.tool_steps,
                attempts=frame.attempts,
                run_id=frame.task_state.run_id,
                task_id=frame.task_state.task_id,
            )
        )
        guidance = "\n".join(
            part
            for part in (hook_decision.guidance, turn_decision.guidance)
            if part
        )
        if guidance:
            frame.ledger.append_guidance(guidance)
        guidance = self._append_budget_guidance(frame, guidance)
        policy_stop = bool(
            hook_decision.stop
            or turn_decision.stop
            or outcome.metadata.get("policy_stop_requested")
        )
        reason = self._policy_reason(hook_decision, turn_decision, outcome)
        if hook_decision.active or turn_decision.active or policy_stop:
            agent.emit_event(
                frame.task_state,
                "policy_decided",
                {
                    "stop": policy_stop,
                    "reason": reason,
                    "guidance": guidance,
                    "tool_call_id": call.call_id,
                },
                correlation_id=call.call_id,
            )
        return guidance, policy_stop, reason

    def _append_budget_guidance(self, frame, guidance):
        agent = self.agent
        if agent.max_steps is None or frame.tool_steps < agent.max_steps:
            return guidance
        budget_guidance = (
            "Runtime tool budget exhausted. Do not call another tool; "
            "use submit_final now with the available evidence."
        )
        frame.ledger.append_guidance(budget_guidance)
        return "\n".join(part for part in (guidance, budget_guidance) if part)

    @staticmethod
    def _policy_reason(hook_decision, turn_decision, outcome):
        outcome_reason = (
            outcome.failure.detail
            if outcome.metadata.get("policy_stop_requested") and outcome.failure
            else ""
        )
        return " | ".join(
            part
            for part in (
                hook_decision.reason,
                turn_decision.reason,
                outcome_reason,
            )
            if part
        )

    def _handle_retry_action(self, frame, turn):
        frame.malformed_retries += 1
        frame.ledger.append_guidance(turn.action.content)
        self._continue_provider(frame, turn, turn.action.content)
        self.agent.run_store.write_task_state(frame.task_state)
        if frame.malformed_retries >= 8:
            return "malformed_model_retry_limit"
        return ""

    def _handle_final_action(self, frame, turn):
        final = turn.action.content.strip()
        blocker = self._static_completion_blocker()
        if blocker:
            status, guidance = blocker
            self._block_completion(frame, turn, status, guidance, guidance)
            return None

        verification_guidance = self._ensure_verification(frame)
        if verification_guidance:
            self._block_completion(
                frame,
                turn,
                "verification_failed",
                verification_guidance,
                verification_guidance,
            )
            return None

        decision = frame.completion_gate.assess()
        if not decision.allowed:
            guidance = (
                f"Runtime completion gate: {decision.reason}. "
                "Inspect or repair before returning a final answer."
            )
            self._block_completion(
                frame,
                turn,
                decision.status,
                decision.reason,
                guidance,
            )
            return None
        return final

    def _static_completion_blocker(self):
        agent = self.agent
        subtask_issue = (
            agent.subagent_manager.completion_issue()
            if agent.subagent_manager is not None
            else ""
        )
        if subtask_issue:
            return "subtasks_incomplete", (
                f"Runtime completion gate: {subtask_issue}."
            )
        syntax_issues = changed_python_syntax_issues(agent)
        if syntax_issues:
            return "syntax_invalid", (
                "Runtime completion gate: changed Python is invalid: "
                + "; ".join(syntax_issues)
            )
        return None

    def _ensure_verification(self, frame):
        agent = self.agent
        preliminary = frame.completion_gate.assess()
        needs_verification = bool(
            (agent.evidence_ledger.changed_paths or not preliminary.allowed)
            and agent.verification_command
        )
        if not needs_verification:
            return ""
        fingerprint = agent.content_workspace_fingerprint()
        verification = agent.evidence_ledger.current_verification(fingerprint)
        if verification is None:
            agent.emit_event(
                frame.task_state,
                "verification_started",
                {"command": agent.verification_command},
            )
            verification = agent.run_verification()
            event = agent.emit_event(
                frame.task_state,
                "verification_finished",
                verification or {"status": "skipped"},
            )
            agent.evidence_ledger.apply_event(event)
        if not verification or verification.get("status") != "passed":
            return (
                "Runtime verification failed; inspect and repair before "
                "submit_final.\n"
                + str((verification or {}).get("output", "verification unavailable"))
            )
        frame.completion_gate.observe_verification(True)
        return ""

    def _block_completion(
        self,
        frame,
        turn,
        status,
        event_reason,
        guidance,
    ):
        frame.ledger.append_guidance(guidance)
        self.agent.emit_event(
            frame.task_state,
            "completion_blocked",
            {"status": status, "reason": event_reason},
        )
        self._continue_provider(frame, turn, guidance)

    def _finish_success(self, frame, final):
        agent = self.agent
        frame.ledger.append_final(final)
        frame.task_state.finish_success(final)
        agent.record_run_summary(frame.task_state)
        self._emit_run_finished(frame, final)
        self._create_checkpoint(frame, "run_finished")
        self._write_terminal_report(frame)
        agent.current_execution.transition("completed")
        agent.current_execution = None
        return final

    def _finish_stopped(self, frame):
        if frame.execution_stop == "step_limit_reached":
            frame.execution_stop = ""
        final = self._apply_stop_state(frame)
        self.agent.record_run_summary(frame.task_state)
        self._emit_run_finished(frame, final)
        self._create_checkpoint(
            frame, frame.task_state.stop_reason or "run_stopped"
        )
        self._write_terminal_report(frame)
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

    def _emit_run_finished(self, frame, final):
        self.agent.emit_event(
            frame.task_state,
            "run_finished",
            {
                "status": frame.task_state.status,
                "stop_reason": frame.task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int(
                    (time.monotonic() - frame.run_started_at) * 1000
                ),
            },
        )

    def _write_terminal_report(self, frame):
        agent = self.agent
        report = agent.redact_artifact(agent.build_report(frame.task_state))
        agent.run_store.write_report(frame.task_state, report)

    def _transition_stopped_execution(self, execution_stop):
        execution = self.agent.current_execution
        if execution is None:
            return
        terminal_state = (
            "cancelled"
            if execution_stop and execution_stop != "deadline_exceeded"
            else "timed_out" if execution_stop else "completed"
        )
        execution.transition(terminal_state, stop_reason=execution_stop)
        self.agent.current_execution = None
