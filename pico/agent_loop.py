"""Model/tool turn control for one Pico request."""

from __future__ import annotations

import json
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
    provider_output_tokens: int | None
    instructions: str
    action_tools: tuple[dict[str, Any], ...]


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
            provider_output_tokens=completion_metadata.get("output_tokens"),
            instructions=prompt.instructions,
            action_tools=tool_surface.tools,
        )

    def _resolve_action_tool_surface(self):
        agent = self.agent
        tools = tuple(agent.tools.model_action_tools())
        if (
            agent.tools.remaining_budget() is not None
            and agent.tools.remaining_budget() == 0
        ):
            tools = tuple(
                tool for tool in tools if tool["name"] == "submit_final"
            )
        return ActionToolSurface(tools=tools)

    def _prepare_prompt(self, loop_state, tool_surface):
        agent = self.agent
        if loop_state.prompt_snapshot is not None:
            _prompt, prior_names = loop_state.prompt_snapshot
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
            compaction_metadata, history_override = self.lifecycle.prepare_compaction(
                loop_state.user_message,
                provider_context_tokens=loop_state.provider_context_tokens,
                action_tools=tool_surface.tools,
            )
            prompt, _metadata = agent.prompt.build(
                loop_state.user_message,
                provider_context_tokens=loop_state.provider_context_tokens,
                compaction_metadata=compaction_metadata,
                history_override=history_override,
                action_tools=tool_surface.tools,
            )
            loop_state.provider_context_tokens = None
            loop_state.prompt_snapshot = (prompt, tool_surface.names)
        else:
            prompt, _names = loop_state.prompt_snapshot
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

    def _continue_provider(self, loop_state, turn, provider_results):
        agent = self.agent
        provider_results = tuple(str(result) for result in provider_results)
        projected_tokens = agent.model_client.projected_context_tokens(
            provider_results,
            instructions=turn.instructions,
            action_tools=turn.action_tools,
            token_counter=agent.prompt.count_tokens,
            provider_input_tokens=turn.provider_input_tokens,
            provider_output_tokens=turn.provider_output_tokens,
        )
        if projected_tokens >= self._provider_high_watermark():
            threshold_tokens = self._provider_high_watermark()
            agent.model_client.reset_action_session()
            loop_state.prompt_snapshot = None
            loop_state.provider_context_tokens = projected_tokens
            agent.emit_event(
                "provider_session_reset",
                {
                    "reason": "context_high_watermark",
                    "input_tokens": turn.provider_input_tokens,
                    "projected_input_tokens": projected_tokens,
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
            agent.tools.remaining_budget() is not None
            and agent.tools.remaining_budget() == 0
        )
        if budget_exhausted and len(calls) == 1:
            return "tool_execution_limit"
        loop_state.invalid_output_count = 0
        loop_state.completion_block_count = 0
        group = agent.run.run_log.append_tool_calls(calls)
        outcomes = agent.tools.execute_pending_group(group.event_id)

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
            agent.tools.remaining_budget() is None
            or agent.tools.remaining_budget() > 0
        ):
            return ""
        budget_instruction = (
            "Runtime tool budget exhausted. Do not call another tool; "
            "use submit_final now with the available evidence."
        )
        agent.append_model_instruction(
            "tool_execution_limit",
            budget_instruction,
        )
        return budget_instruction

    def _handle_invalid_output(self, loop_state, turn):
        loop_state.invalid_output_count += 1
        self.agent.append_model_instruction(
            "invalid_model_output",
            turn.action.content,
        )
        self._continue_provider(loop_state, turn, (turn.action.content,))
        if loop_state.invalid_output_count >= 8:
            return "invalid_output_limit"
        return ""

    def _handle_final_action(self, loop_state, turn):
        assessment = self.completion.assess(turn.action.content.strip())
        if assessment.allowed:
            return assessment.instruction
        self._block_completion(
            loop_state,
            turn,
            assessment.status,
            assessment.instruction,
            assessment.evidence,
        )
        return None

    def _block_completion(
        self,
        loop_state,
        turn,
        status,
        instruction,
        evidence,
    ):
        self.agent.append_model_instruction(
            status,
            instruction,
            evidence=evidence,
        )
        self.agent.emit_event(
            "completion_blocked",
            {
                "status": status,
                "instruction": instruction,
                "evidence": evidence,
            },
        )
        loop_state.completion_block_count += 1
        if loop_state.completion_block_count >= 3:
            loop_state.execution_stop = "completion_block_limit"
            return
        feedback = {
            "runtime_instruction": {
                "code": status,
                "instruction": instruction,
            },
            "untrusted_evidence": evidence,
        }
        self._continue_provider(
            loop_state,
            turn,
            (json.dumps(feedback, ensure_ascii=False, sort_keys=True),),
        )
