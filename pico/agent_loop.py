"""Model/tool turn control for one Pico request."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .completion_controller import CompletionController
from .hooks import AfterToolContext, TurnContext
from .run_lifecycle import RunLifecycle

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class ModelTurn:
    action: Any
    prompt_metadata: dict[str, Any]
    provider_input_tokens: int | None
    provider_output_tokens: int | None


class AgentLoop:
    def __init__(self, agent: Pico):
        self.agent = agent
        self.lifecycle = RunLifecycle(agent)
        self.completion = CompletionController(agent)

    def run(self, user_message):
        frame = self.lifecycle.initialize(user_message)
        while True:
            frame.execution_stop = self.lifecycle.execution_stop()
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
                    return self.lifecycle.finish_success(frame, final)
            if frame.execution_stop:
                break
        return self.lifecycle.finish_stopped(frame)

    def _next_model_turn(self, frame):
        agent = self.agent
        frame.attempts += 1
        frame.task_state.record_attempt()
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
        provider_output_tokens = completion_metadata.get("output_tokens")
        return ModelTurn(
            action=action,
            prompt_metadata=prompt_metadata,
            provider_input_tokens=provider_input_tokens,
            provider_output_tokens=provider_output_tokens,
        )

    def _prepare_prompt(self, frame):
        agent = self.agent
        prompt_reused = frame.prompt_snapshot is not None
        if frame.prompt_snapshot is None:
            prompt, prompt_metadata = agent.prompt.build(frame.user_message)
            frame.prompt_snapshot = (prompt, dict(prompt_metadata))
        else:
            prompt, original_metadata = frame.prompt_snapshot
            prompt_metadata = dict(original_metadata)
        prompt_metadata["prompt_reused"] = prompt_reused
        prompt_metadata["provider_session_active"] = prompt_reused
        self._record_prompt_projection(frame, prompt_metadata, prompt_reused)
        return prompt, prompt_metadata

    def _record_prompt_projection(self, frame, prompt_metadata, prompt_reused):
        if prompt_reused:
            return
        agent = self.agent
        next_generation = int(
            prompt_metadata.get("journal_generation", frame.journal.generation)
        )
        frame.context_generation = max(frame.context_generation, next_generation)
        memory_audit = dict(prompt_metadata.get("memory_retrieval", {}) or {})
        if memory_audit.get("available_count") or memory_audit.get(
            "selected_filenames"
        ):
            agent.emit_event(frame.task_state, "memory_selection", memory_audit)

    def _request_action(self, frame, prompt, prompt_metadata):
        agent = self.agent
        prompt_cache_key = (
            prompt_metadata.get("prompt_cache_key")
            if getattr(agent.model_client, "supports_prompt_cache", False)
            else None
        )
        action_tools = (
            [
                tool
                for tool in agent.tools.action_schemas
                if tool["name"] == "submit_final"
            ]
            if agent.config.max_steps is not None
            and frame.tool_steps >= agent.config.max_steps
            else agent.tools.action_schemas
        )
        model_started_at = time.monotonic()
        action = agent.model_client.complete_action(
            prompt,
            agent.config.max_new_tokens,
            action_tools=action_tools,
            prompt_cache_key=prompt_cache_key,
            request_timeout=agent.run.execution.bounded_timeout(),
        )
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.run.last_completion_metadata = completion_metadata
        agent.run.last_prompt_metadata = prompt_metadata
        agent.emit_event(
            frame.task_state,
            "turn_metrics",
            {
                "kind": action.kind,
                "tool_call_id": action.tool_call.call_id if action.tool_call else "",
                "completion_metadata": completion_metadata,
                "prompt_metadata": prompt_metadata,
                "prompt_reused": bool(prompt_metadata.get("prompt_reused")),
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
            },
        )
        return action, completion_metadata

    def _should_rotate_provider(self, turn, provider_result):
        input_tokens = turn.provider_input_tokens
        if not isinstance(input_tokens, int):
            return False
        output_tokens = (
            turn.provider_output_tokens
            if isinstance(turn.provider_output_tokens, int)
            else 0
        )
        result_tokens = self.agent.prompt.context.tokenizer.count(provider_result)
        estimated_next_total = (
            input_tokens
            + output_tokens
            + result_tokens
            + self.agent.config.max_new_tokens
        )
        return estimated_next_total >= self.agent.config.provider_context_limit_tokens

    def _continue_provider(self, frame, turn, provider_result, tool_call_id=""):
        agent = self.agent
        if self._should_rotate_provider(turn, provider_result):
            output_tokens = (
                turn.provider_output_tokens
                if isinstance(turn.provider_output_tokens, int)
                else 0
            )
            result_tokens = agent.prompt.context.tokenizer.count(provider_result)
            estimated_next_total = (
                turn.provider_input_tokens
                + output_tokens
                + result_tokens
                + agent.config.max_new_tokens
            )
            agent.model_client.reset_action_session()
            frame.prompt_snapshot = None
            agent.emit_event(
                frame.task_state,
                "provider_session_reset",
                {
                    "reason": "next_input_threshold",
                    "input_tokens": turn.provider_input_tokens,
                    "output_tokens": output_tokens,
                    "tool_result_tokens": result_tokens,
                    "estimated_next_total": estimated_next_total,
                    "tool_call_id": tool_call_id,
                },
            )
            return
        agent.model_client.record_action_result(turn.action, provider_result)

    def _handle_tool_action(self, frame, turn):
        agent = self.agent
        if (
            agent.config.max_steps is not None
            and frame.tool_steps >= agent.config.max_steps
        ):
            return "step_limit_reached"
        frame.malformed_retries = 0
        call = turn.action.tool_call
        frame.journal.append_tool_call(call)
        outcome = agent.tools.run(call)
        if outcome.execution_state != "not_started":
            frame.tool_steps += 1
            frame.task_state.record_tool(call.name)
        frame.completion_gate.observe(outcome)

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
        if policy_stop:
            return "policy:" + (reason or "runtime policy requested stop")
        return ""

    def _tool_policy(self, frame, call, outcome):
        agent = self.agent
        hook_decision = agent.services.hooks.after_tool_result(
            AfterToolContext(
                outcome=outcome,
                tool_steps=frame.tool_steps,
                run_id=frame.task_state.run_id,
                task_id=frame.task_state.task_id,
            )
        )
        turn_decision = agent.services.hooks.should_stop_after_turn(
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
            frame.journal.append_guidance(guidance)
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
            )
        return guidance, policy_stop, reason

    def _append_budget_guidance(self, frame, guidance):
        agent = self.agent
        if (
            agent.config.max_steps is None
            or frame.tool_steps < agent.config.max_steps
        ):
            return guidance
        budget_guidance = (
            "Runtime tool budget exhausted. Do not call another tool; "
            "use submit_final now with the available evidence."
        )
        frame.journal.append_guidance(budget_guidance)
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
        frame.journal.append_guidance(turn.action.content)
        self._continue_provider(frame, turn, turn.action.content)
        if frame.malformed_retries >= 8:
            return "malformed_model_retry_limit"
        return ""

    def _handle_final_action(self, frame, turn):
        assessment = self.completion.assess(frame, turn.action.content.strip())
        if assessment.allowed:
            return assessment.final
        self._block_completion(
            frame,
            turn,
            assessment.status,
            assessment.reason,
            assessment.guidance,
        )
        return None

    def _block_completion(
        self,
        frame,
        turn,
        status,
        event_reason,
        guidance,
    ):
        frame.journal.append_guidance(guidance)
        self.agent.emit_event(
            frame.task_state,
            "completion_blocked",
            {"status": status, "reason": event_reason},
        )
        self._continue_provider(frame, turn, guidance)
