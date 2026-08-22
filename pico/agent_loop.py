"""Model/tool turn control for one Pico request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .completion_controller import CompletionController
from .run_lifecycle import RunLifecycle

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class ModelTurn:
    action: Any
    provider_input_tokens: int | None
    provider_output_tokens: int | None
    provider_total_tokens: int | None


class AgentLoop:
    def __init__(self, agent: Pico):
        self.agent = agent
        self.lifecycle = RunLifecycle(agent)
        self.completion = CompletionController(agent)

    def run(self, user_message):
        loop_state = self.lifecycle.initialize(user_message)
        while True:
            loop_state.execution_stop = self.lifecycle.execution_stop()
            if loop_state.execution_stop:
                break

            try:
                turn = self._next_model_turn(loop_state)
            except RuntimeError as exc:
                if self._recover_context_overflow(loop_state, exc):
                    continue
                raise
            if turn.action.kind == "tool":
                loop_state.execution_stop = self._handle_tool_action(loop_state, turn)
            elif turn.action.kind == "invalid":
                loop_state.execution_stop = self._handle_invalid_output(loop_state, turn)
            else:
                final = self._handle_final_action(loop_state, turn)
                if final is not None:
                    return self.lifecycle.finish_success(loop_state, final)
            if loop_state.execution_stop:
                break
        return self.lifecycle.finish_stopped(loop_state)

    def _next_model_turn(self, loop_state):
        agent = self.agent
        prompt, prompt_metadata = self._prepare_prompt(loop_state)
        agent.emit_event(
            "model_requested",
            {
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
            },
        )
        action, completion_metadata = self._request_action(
            loop_state, prompt, prompt_metadata
        )
        provider_input_tokens = completion_metadata.get("input_tokens")
        provider_output_tokens = completion_metadata.get("output_tokens")
        provider_total_tokens = completion_metadata.get("total_tokens")
        loop_state.overflow_recovery_attempted = False
        return ModelTurn(
            action=action,
            provider_input_tokens=provider_input_tokens,
            provider_output_tokens=provider_output_tokens,
            provider_total_tokens=provider_total_tokens,
        )

    def _prepare_prompt(self, loop_state):
        agent = self.agent
        prompt_reused = loop_state.prompt_snapshot is not None
        if loop_state.prompt_snapshot is None:
            prompt, prompt_metadata = agent.prompt.build(
                loop_state.user_message,
                provider_context_tokens=loop_state.provider_context_tokens,
                provider_overhead_tokens=loop_state.provider_overhead_tokens,
            )
            loop_state.provider_context_tokens = None
            loop_state.prompt_snapshot = (prompt, dict(prompt_metadata))
        else:
            prompt, original_metadata = loop_state.prompt_snapshot
            prompt_metadata = dict(original_metadata)
        prompt_metadata["prompt_reused"] = prompt_reused
        return prompt, prompt_metadata

    def _request_action(self, loop_state, prompt, prompt_metadata):
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
            if agent.config.max_tool_executions is not None
            and agent.run.task_state.executed_tool_count
            >= agent.config.max_tool_executions
            else agent.tools.action_schemas
        )
        action = agent.model_client.complete_action(
            prompt,
            agent.config.max_new_tokens,
            action_tools=action_tools,
            prompt_cache_key=prompt_cache_key,
            request_timeout=agent.run.execution_context.bounded_timeout(),
        )
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        self._observe_provider_overhead(
            loop_state,
            prompt_metadata,
            completion_metadata,
        )
        persisted_prompt_metadata = self._persisted_prompt_metadata(prompt_metadata)
        agent.emit_event(
            "turn_metrics",
            {
                "completion_metadata": completion_metadata,
                "prompt_metadata": persisted_prompt_metadata,
                "prompt_reused": bool(prompt_metadata.get("prompt_reused")),
            },
        )
        return action, completion_metadata

    @staticmethod
    def _observe_provider_overhead(
        loop_state,
        prompt_metadata,
        completion_metadata,
    ):
        if prompt_metadata.get("prompt_reused"):
            return
        input_tokens = completion_metadata.get("input_tokens")
        if not isinstance(input_tokens, int) or input_tokens < 0:
            return
        known_input_tokens = int(prompt_metadata.get("prompt_tokens") or 0) + int(
            prompt_metadata.get("tool_schema_tokens") or 0
        )
        observed = max(0, input_tokens - known_input_tokens)
        loop_state.provider_overhead_tokens = observed
        prompt_metadata["observed_provider_overhead_tokens"] = observed

    @staticmethod
    def _persisted_prompt_metadata(prompt_metadata):
        if not prompt_metadata.get("prompt_reused"):
            persisted = dict(prompt_metadata)
            persisted.pop("prompt_reused", None)
            return persisted
        keys = (
            "prompt_cache_key",
            "run_log_generation",
            "provider_context_tokens",
            "tool_schema_tokens",
            "provider_overhead_tokens",
            "estimated_input_tokens",
            "observed_provider_overhead_tokens",
        )
        return {key: prompt_metadata.get(key) for key in keys}

    def _context_tokens_after_result(self, turn, provider_result):
        input_tokens = turn.provider_input_tokens
        if not isinstance(input_tokens, int):
            return None
        output_tokens = (
            turn.provider_output_tokens
            if isinstance(turn.provider_output_tokens, int)
            else 0
        )
        base_tokens = (
            turn.provider_total_tokens
            if isinstance(turn.provider_total_tokens, int)
            and turn.provider_total_tokens > 0
            else input_tokens + output_tokens
        )
        result_tokens = self.agent.prompt.context.tokenizer.count(provider_result)
        return base_tokens + result_tokens

    def _should_rotate_provider(self, turn, provider_result):
        context_tokens = self._context_tokens_after_result(turn, provider_result)
        if context_tokens is None:
            return False
        return (
            context_tokens + self.agent.config.max_new_tokens
            >= self.agent.config.provider_context_limit_tokens
        )

    def _continue_provider(self, loop_state, turn, provider_result, tool_call_id=""):
        agent = self.agent
        if self._should_rotate_provider(turn, provider_result):
            output_tokens = (
                turn.provider_output_tokens
                if isinstance(turn.provider_output_tokens, int)
                else 0
            )
            result_tokens = agent.prompt.context.tokenizer.count(provider_result)
            context_tokens = self._context_tokens_after_result(turn, provider_result)
            estimated_next_total = context_tokens + agent.config.max_new_tokens
            agent.model_client.reset_action_session()
            loop_state.prompt_snapshot = None
            loop_state.provider_context_tokens = context_tokens
            agent.emit_event(
                "provider_session_reset",
                {
                    "reason": "next_input_threshold",
                    "input_tokens": turn.provider_input_tokens,
                    "output_tokens": output_tokens,
                    "tool_result_tokens": result_tokens,
                    "estimated_next_total": estimated_next_total,
                    "provider_context_tokens": context_tokens,
                    "tool_call_id": tool_call_id,
                },
            )
            return
        agent.model_client.record_action_result(turn.action, provider_result)

    @staticmethod
    def _is_context_overflow_error(exc):
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "context window",
                "maximum context length",
                "max context length",
                "prompt is too long",
                "input is too long",
                "too many tokens",
                "token limit exceeded",
            )
        )

    def _recover_context_overflow(self, loop_state, exc):
        if (
            loop_state.overflow_recovery_attempted
            or not self._is_context_overflow_error(exc)
        ):
            return False
        loop_state.overflow_recovery_attempted = True
        loop_state.prompt_snapshot = None
        loop_state.provider_context_tokens = (
            self.agent.config.provider_context_limit_tokens
        )
        self.agent.model_client.reset_action_session()
        self.agent.emit_event(
            "provider_session_reset",
            {
                "reason": "context_overflow_retry",
                "provider_context_tokens": loop_state.provider_context_tokens,
                "tool_call_id": "",
            },
        )
        return True

    def _handle_tool_action(self, loop_state, turn):
        agent = self.agent
        if (
            agent.config.max_tool_executions is not None
            and agent.run.task_state.executed_tool_count
            >= agent.config.max_tool_executions
        ):
            return "tool_execution_limit"
        loop_state.invalid_output_count = 0
        loop_state.completion_block_count = 0
        call = turn.action.tool_call
        agent.apply_run_event(agent.run.run_log.append_tool_call(call))
        outcome = agent.tools.run(call)

        model_instruction = self._append_budget_instruction(loop_state)
        provider_result = outcome.content
        if model_instruction:
            provider_result += "\n\nRuntime instruction: " + model_instruction
        self._continue_provider(
            loop_state,
            turn,
            provider_result,
            tool_call_id=call.call_id,
        )
        return ""

    def _append_budget_instruction(self, loop_state):
        agent = self.agent
        if (
            agent.config.max_tool_executions is None
            or agent.run.task_state.executed_tool_count
            < agent.config.max_tool_executions
        ):
            return ""
        budget_instruction = (
            "Runtime tool budget exhausted. Do not call another tool; "
            "use submit_final now with the available evidence."
        )
        agent.apply_run_event(
            agent.run.run_log.append_model_instruction(budget_instruction)
        )
        return budget_instruction

    def _handle_invalid_output(self, loop_state, turn):
        loop_state.invalid_output_count += 1
        self.agent.apply_run_event(
            self.agent.run.run_log.append_model_instruction(turn.action.content)
        )
        self._continue_provider(loop_state, turn, turn.action.content)
        if loop_state.invalid_output_count >= 8:
            return "invalid_output_limit"
        return ""

    def _handle_final_action(self, loop_state, turn):
        assessment = self.completion.assess(turn.action.content.strip())
        if assessment.allowed:
            return assessment.final_answer
        self._block_completion(
            loop_state,
            turn,
            assessment.status,
            assessment.instruction,
        )
        return None

    def _block_completion(
        self,
        loop_state,
        turn,
        status,
        instruction,
    ):
        self.agent.apply_run_event(
            self.agent.run.run_log.append_model_instruction(instruction)
        )
        self.agent.emit_event(
            "completion_blocked",
            {"status": status, "reason": instruction},
        )
        loop_state.completion_block_count += 1
        if loop_state.completion_block_count >= 3:
            loop_state.execution_stop = "completion_block_limit"
            return
        self._continue_provider(loop_state, turn, instruction)
