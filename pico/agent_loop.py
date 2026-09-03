"""Model/tool turn control for one Pico request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .completion_controller import CompletionController
from .providers import ProviderContextOverflow
from .run_lifecycle import RunLifecycle, reload_current_run
from .run_projection import RunOutcome

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class ModelTurn:
    action: Any
    provider_input_tokens: int | None


@dataclass(frozen=True)
class ActionToolSurface:
    """One turn's model-visible and Runtime-allowed tool surface."""

    tools: tuple[dict[str, Any], ...]

    @property
    def names(self):
        return tuple(str(tool["name"]) for tool in self.tools)


class AgentLoop:
    def __init__(self, agent: Pico):
        self.agent = agent
        self.lifecycle = RunLifecycle(agent)
        self.completion = CompletionController(agent)

    def run(
        self,
        user_message,
    ) -> RunOutcome:
        loop_state = self.lifecycle.initialize(
            user_message,
        )
        try:
            while True:
                loop_state.execution_stop = self.lifecycle.execution_stop()
                if loop_state.execution_stop:
                    break
                if (
                    self.agent.run.metrics.model_request_count
                    - loop_state.starting_model_request_count
                    >= self.agent.config.max_agent_turns
                ):
                    loop_state.execution_stop = "agent_turn_limit"
                    break

                try:
                    turn = self._next_model_turn(loop_state)
                except ProviderContextOverflow:
                    if self._recover_context_overflow(loop_state):
                        continue
                    raise
                except BaseException:
                    loop_state.execution_stop = self.lifecycle.execution_stop()
                    if loop_state.execution_stop:
                        break
                    raise
                loop_state.execution_stop = self.lifecycle.execution_stop()
                if loop_state.execution_stop:
                    break
                if turn.action.kind == "tool":
                    loop_state.execution_stop = self._handle_tool_turn(
                        loop_state, turn
                    )
                elif turn.action.kind == "invalid":
                    loop_state.execution_stop = self._handle_invalid_output(
                        loop_state, turn
                    )
                else:
                    final = self._handle_final_action(loop_state, turn)
                    if final is not None:
                        return self.lifecycle.finish_success(loop_state, final)
                if loop_state.execution_stop:
                    break
            return self.lifecycle.finish_stopped(loop_state)
        except BaseException:
            self.agent.run.execution_context = None
            reload_current_run(self.agent)
            raise

    def _next_model_turn(self, loop_state):
        agent = self.agent
        tool_surface = self._resolve_action_tool_surface()
        prompt = self._prepare_prompt(loop_state, tool_surface)
        agent.emit_event("model_requested")
        action, completion_metadata = self._request_action(
            loop_state,
            prompt,
            tool_surface,
        )
        provider_input_tokens = completion_metadata.get("input_tokens")
        loop_state.overflow_recovery_attempted = False
        return ModelTurn(
            action=action,
            provider_input_tokens=provider_input_tokens,
        )

    def _resolve_action_tool_surface(self):
        agent = self.agent
        tools = tuple(agent.tools.model_action_tools())
        if (
            agent.config.max_tool_executions is not None
            and agent.run.metrics.executed_tool_count
            >= agent.config.max_tool_executions
        ):
            tools = tuple(
                tool for tool in tools if tool["name"] == "submit_final"
            )
        return ActionToolSurface(tools=tools)

    def _prepare_prompt(self, loop_state, tool_surface):
        agent = self.agent
        if loop_state.prompt_snapshot is not None:
            _prompt, snapshot_metadata = loop_state.prompt_snapshot
            prior_names = tuple(snapshot_metadata.get("tool_names", ()))
            if prior_names != tool_surface.names:
                agent.model_client.reset_action_session()
                loop_state.prompt_snapshot = None
                loop_state.provider_context_tokens = None
                agent.emit_event(
                    "provider_session_reset",
                    {
                        "reason": "tool_surface_changed",
                        "tool_names": list(tool_surface.names),
                    },
                )
        if loop_state.prompt_snapshot is None:
            compaction_metadata, history_override = agent.prompt.prepare_compaction(
                loop_state.user_message,
                provider_context_tokens=loop_state.provider_context_tokens,
                action_tools=tool_surface.tools,
            )
            prompt, prompt_metadata = agent.prompt.build(
                loop_state.user_message,
                provider_context_tokens=loop_state.provider_context_tokens,
                compaction_metadata=compaction_metadata,
                history_override=history_override,
                action_tools=tool_surface.tools,
            )
            prompt_metadata["tool_names"] = list(tool_surface.names)
            loop_state.provider_context_tokens = None
            loop_state.prompt_snapshot = (prompt, dict(prompt_metadata))
        else:
            prompt, original_metadata = loop_state.prompt_snapshot
            prompt_metadata = dict(original_metadata)
        prompt_metadata["tool_names"] = list(tool_surface.names)
        return prompt

    def _request_action(
        self,
        loop_state,
        prompt,
        tool_surface,
    ):
        agent = self.agent
        action = agent.model_client.complete_action(
            prompt.input_text,
            agent.config.max_new_tokens,
            instructions=prompt.instructions,
            action_tools=tool_surface.tools,
            request_timeout=agent.run.execution_context.bounded_timeout(),
        )
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        agent.emit_event(
            "turn_metrics",
            {
                "input_tokens": completion_metadata.get("input_tokens"),
                "output_tokens": completion_metadata.get("output_tokens"),
            },
        )
        return action, completion_metadata

    def _provider_high_watermark(self):
        config = self.agent.config
        return (
            config.provider_context_limit_tokens
            - config.compaction_reserve_tokens
        )

    def _should_rotate_provider(self, turn):
        return isinstance(turn.provider_input_tokens, int) and (
            turn.provider_input_tokens >= self._provider_high_watermark()
        )

    def _continue_provider(self, loop_state, turn, provider_results):
        agent = self.agent
        provider_results = tuple(str(result) for result in provider_results)
        if self._should_rotate_provider(turn):
            threshold_tokens = self._provider_high_watermark()
            agent.model_client.reset_action_session()
            loop_state.prompt_snapshot = None
            loop_state.provider_context_tokens = turn.provider_input_tokens
            agent.emit_event(
                "provider_session_reset",
                {
                    "reason": "context_high_watermark",
                    "input_tokens": turn.provider_input_tokens,
                    "threshold_tokens": threshold_tokens,
                },
            )
            return
        agent.model_client.record_action_results(provider_results)

    def _recover_context_overflow(self, loop_state):
        if loop_state.overflow_recovery_attempted:
            return False
        loop_state.overflow_recovery_attempted = True
        loop_state.prompt_snapshot = None
        loop_state.provider_context_tokens = (
            self.agent.config.provider_context_limit_tokens
        )
        self.agent.model_client.reset_action_session()
        self.agent.emit_event(
            "provider_session_reset",
            {"reason": "context_overflow_retry"},
        )
        return True

    def _handle_tool_turn(self, loop_state, turn):
        agent = self.agent
        calls = turn.action.tool_calls
        budget_exhausted = (
            agent.config.max_tool_executions is not None
            and agent.run.metrics.executed_tool_count
            >= agent.config.max_tool_executions
        )
        if budget_exhausted and len(calls) == 1:
            return "tool_execution_limit"
        loop_state.invalid_output_count = 0
        loop_state.completion_block_count = 0
        if len(calls) == 1:
            call = calls[0]
            agent.apply_run_event(agent.run.run_log.append_tool_call(call))
            outcomes = (agent.tools.execute_pending(call.call_id),)
        else:
            batch = agent.apply_run_event(
                agent.run.run_log.append_tool_batch(calls)
            )
            outcomes = agent.tools.execute_pending_batch(batch.batch_id)

        model_instruction = self._append_budget_instruction(loop_state)
        provider_results = [outcome.render_for_model() for outcome in outcomes]
        if model_instruction:
            provider_results[-1] += (
                "\n\nRuntime instruction: " + model_instruction
            )
        self._continue_provider(
            loop_state,
            turn,
            provider_results,
        )
        return "tool_execution_limit" if budget_exhausted else ""

    def _append_budget_instruction(self, loop_state):
        agent = self.agent
        if (
            agent.config.max_tool_executions is None
            or agent.run.metrics.executed_tool_count
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
        self._continue_provider(loop_state, turn, (turn.action.content,))
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
        self._continue_provider(loop_state, turn, (instruction,))
