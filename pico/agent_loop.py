"""Model/tool turn control for one Pico request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .completion_controller import CompletionController
from .prompt_builder import render_runtime_feedback
from .providers import ProviderContextOverflow
from .run_lifecycle import RunLifecycle, reload_current_run
from .run_projection import RunOutcome

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class ModelTurn:
    action: Any
    instructions: str
    tool_surface: Any


@dataclass(slots=True)
class LoopDirective:
    """One internal decision returned to the sole Agent loop orchestrator."""

    kind: Literal["continue", "complete", "stop"]
    detail: str = ""


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
        directive = LoopDirective("continue")
        try:
            while directive.kind == "continue":
                directive = self._step(loop_state)
            return self._settle(loop_state, directive)
        except BaseException:
            self.agent.run.execution_context = None
            reload_current_run(self.agent)
            raise

    def _step(self, loop_state):
        """One model decision: execute a tool, correct output, or verify completion."""
        stop = self.lifecycle.execution_stop()
        if stop:
            return LoopDirective("stop", stop)
        used = self.agent.run.metrics.model_request_count - loop_state.starting_model_request_count
        if used >= self.agent.config.max_agent_turns:
            return LoopDirective("stop", "agent_turn_limit")
        try:
            turn = self._next_model_turn(loop_state)
            stop = self.lifecycle.execution_stop()
            if stop:
                return LoopDirective("stop", stop)
            if turn.action.kind == "tool":
                return self._handle_tool_turn(loop_state, turn)
            if turn.action.kind == "invalid":
                return self._handle_invalid_output(loop_state, turn)
            return self._handle_final_action(loop_state, turn)
        except ProviderContextOverflow:
            if self._recover_context_overflow(loop_state):
                return LoopDirective("continue")
            raise
        except BaseException:
            stop = self.lifecycle.execution_stop()
            if not stop:
                raise
            return LoopDirective("stop", stop)

    def _settle(self, loop_state, directive):
        if directive.kind == "complete":
            try:
                return self.lifecycle.finish_success(
                    directive.detail,
                    run_started_at=loop_state.run_started_at,
                )
            except BaseException:
                stop_reason = self.lifecycle.execution_stop()
                if not stop_reason:
                    raise
                directive = LoopDirective("stop", stop_reason)
        if directive.kind == "stop":
            return self.lifecycle.finish_stopped(
                directive.detail,
                run_started_at=loop_state.run_started_at,
            )
        raise RuntimeError("Agent loop exited without a terminal directive")

    def _next_model_turn(self, loop_state):
        agent = self.agent
        tool_surface = agent.tools.resolve_surface()
        prompt = self._prepare_prompt(loop_state, tool_surface)
        action = self._request_action(
            loop_state,
            prompt,
            tool_surface,
        )
        loop_state.overflow_recovery_attempted = False
        return ModelTurn(
            action=action,
            instructions=prompt.instructions,
            tool_surface=tool_surface,
        )

    def _prepare_prompt(self, loop_state, tool_surface):
        agent = self.agent
        if loop_state.prompt_snapshot is not None:
            _prompt, prior_surface = loop_state.prompt_snapshot
            if prior_surface.names != tool_surface.names or prior_surface.policy != tool_surface.policy:
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
            inputs = self.lifecycle.prepare_compaction(
                loop_state.user_message,
                tool_surface=tool_surface,
                provider_context_tokens=loop_state.provider_context_tokens,
            )
            prompt = agent.prompt.build(inputs)
            loop_state.provider_context_tokens = None
            loop_state.prompt_snapshot = (prompt, tool_surface)
        else:
            prompt, _surface = loop_state.prompt_snapshot
        return prompt

    def _request_action(
        self,
        loop_state,
        prompt,
        tool_surface,
    ):
        agent = self.agent
        input_tokens = agent.model_client.estimate_action_input_tokens(
            prompt.input_text, instructions=prompt.instructions,
            action_tools=tool_surface.action_tools, token_counter=agent.prompt.count_tokens,
        )
        if loop_state.overflow_recovery_attempted and input_tokens >= loop_state.last_request_input_tokens:
            raise ProviderContextOverflow(
                "context overflow recovery did not reduce the full request "
                f"({loop_state.last_request_input_tokens} -> {input_tokens} estimated input tokens); "
                "check the model context/output limits or reduce required context"
            )
        loop_state.last_request_input_tokens = input_tokens
        agent.emit_event("model_requested")
        action = agent.model_client.complete_action(
            prompt.input_text,
            agent.config.max_new_tokens,
            instructions=prompt.instructions,
            action_tools=tool_surface.action_tools,
            execution_context=agent.run.execution_context,
        )
        completion_metadata = dict(
            getattr(agent.model_client, "last_completion_metadata", {}) or {}
        )
        agent.emit_event(
            "turn_metrics",
            {
                "input_tokens": completion_metadata.get("input_tokens"),
                "cached_tokens": completion_metadata.get("cached_tokens"),
                "output_tokens": completion_metadata.get("output_tokens"),
            },
        )
        return action

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
            action_tools=turn.tool_surface.action_tools,
            token_counter=agent.prompt.count_tokens,
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
                    "input_tokens": agent.model_client.last_completion_metadata.get("input_tokens"),
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
        call = turn.action.tool_call
        if call is None:
            raise RuntimeError("tool turn is missing its tool call")
        loop_state.invalid_output_count = 0
        loop_state.completion_block_count = 0
        if agent.prompt.refresh_repository_instructions():
            agent.tools._rejected(
                call, "repository_instructions_changed",
                "The tool was not executed. Repository instructions were loaded or "
                "changed; review their directory scope and propose the call again.",
                recovery="retry_after_change", record=True,
            )
            agent.model_client.reset_action_session()
            loop_state.prompt_snapshot = None
            loop_state.provider_context_tokens = None
            agent.emit_event("provider_session_reset", {
                "reason": "repository_instructions_changed",
            })
            return LoopDirective("continue")
        outcome = agent.tools.execute_call(call, turn.tool_surface)

        self._continue_provider(
            loop_state,
            turn,
            (outcome.render_for_model(),),
        )
        return LoopDirective("continue")

    def _handle_invalid_output(self, loop_state, turn):
        loop_state.invalid_output_count += 1
        self.agent.append_model_instruction(
            turn.action.content,
        )
        self._continue_provider(loop_state, turn, (turn.action.content,))
        if loop_state.invalid_output_count >= 8:
            return LoopDirective("stop", "invalid_output_limit")
        return LoopDirective("continue")

    def _handle_final_action(self, loop_state, turn):
        final = turn.action.content.strip()
        verification_policy = self.completion.resolve_verification_policy()
        assessment = self.completion.assess(final, verification_policy)
        if assessment.verification_required:
            self.lifecycle.run_completion_verification(verification_policy)
            assessment = self.completion.assess_verification(
                final,
                verification_policy,
            )
        if assessment.allowed:
            return LoopDirective("complete", assessment.instruction)
        return self._block_completion(
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
            instruction,
            evidence=evidence,
        )
        self.agent.emit_event(
            "completion_blocked",
            {"status": status},
        )
        loop_state.completion_block_count += 1
        if loop_state.completion_block_count >= 3:
            return LoopDirective("stop", "completion_block_limit")
        self._continue_provider(
            loop_state,
            turn,
            (render_runtime_feedback(self.agent.run.projection.runtime_feedback),),
        )
        return LoopDirective("continue")
