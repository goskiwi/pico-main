"""Model/tool turn control for one Pico request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .failure_policy import guidance_for_failure
from .providers import ProviderContextOverflow
from .run_lifecycle import RunLifecycle, reload_current_run
from .run_projection import RunOutcome

if TYPE_CHECKING:
    from .prompt_builder import ModelPrompt
    from .runtime import Pico


@dataclass
class AgentLoopState:
    prompt_snapshot: ModelPrompt | None = None
    force_compaction: bool = False
    overflow_recovery_attempted: bool = False
    last_request_input_tokens: int = 0
    invalid_output_count: int = 0
    model_request_count_at_start: int = 0


@dataclass(slots=True)
class LoopDirective:
    """One internal decision returned to the sole Agent loop orchestrator."""

    kind: Literal["continue", "complete", "stop"]
    detail: Any = ""


class AgentLoop:
    def __init__(self, agent: Pico):
        self.agent = agent
        self.lifecycle = RunLifecycle(agent)

    def run(
        self,
        user_message,
    ) -> RunOutcome:
        attempt_started_at = self.lifecycle.initialize(user_message)
        loop_state = AgentLoopState(
            model_request_count_at_start=self.agent.run.metrics.model_request_count,
        )
        directive = LoopDirective("continue")
        try:
            tool_surface = self.agent.tools.resolve_surface()
            while directive.kind == "continue":
                directive = self._step(loop_state, tool_surface)
            return self._settle(
                directive,
                attempt_started_at=attempt_started_at,
            )
        except BaseException:
            self.agent.run.execution_context = None
            reload_current_run(self.agent)
            raise

    def _step(self, loop_state, tool_surface):
        """One model decision: execute a tool, correct output, or complete."""
        stop = self.lifecycle.execution_stop()
        if stop:
            return LoopDirective("stop", stop)
        used = (
            self.agent.run.metrics.model_request_count
            - loop_state.model_request_count_at_start
        )
        if used >= self.agent.config.max_model_requests_per_attempt:
            return LoopDirective("stop", "model_request_limit")
        try:
            prompt = self._prepare_prompt(loop_state, tool_surface)
            turn = self._request_turn(loop_state, prompt, tool_surface)
            loop_state.overflow_recovery_attempted = False
            stop = self.lifecycle.execution_stop()
            if stop:
                self._close_interrupted_model_request(stop)
                return LoopDirective("stop", stop)
            action = turn.action
            if action.kind == "tool":
                self.agent.run.run_log.append_assistant_turn(
                    self.agent.redact_turn(turn)
                )
                return self._handle_tool_turn(
                    loop_state,
                    action,
                    prompt,
                    tool_surface,
                )
            if action.kind not in {"tool", "final"}:
                return self._handle_model_failure(
                    loop_state,
                    action,
                    prompt,
                    tool_surface,
                )
            return self._handle_final_action(loop_state, turn)
        except ProviderContextOverflow:
            self.agent.run.run_log.append_model_failure(
                "context_overflow",
                "provider context window exceeded",
                "provider context window exceeded",
                getattr(self.agent.model_client, "last_completion_metadata", {}) or {},
            )
            if self._recover_context_overflow(loop_state):
                return LoopDirective("continue")
            raise
        except BaseException:
            stop = self.lifecycle.execution_stop()
            if not stop:
                raise
            self._close_interrupted_model_request(stop)
            return LoopDirective("stop", stop)

    def _close_interrupted_model_request(self, stop_reason):
        if self.agent.run.projection.phase != "requesting_model":
            return
        self.agent.run.run_log.append_model_failure(
            "interrupted",
            str(stop_reason),
            str(stop_reason),
            getattr(self.agent.model_client, "last_completion_metadata", {}) or {},
        )

    def _settle(self, directive, *, attempt_started_at):
        if directive.kind == "complete":
            try:
                return self.lifecycle.finish_success(
                    directive.detail,
                    attempt_started_at=attempt_started_at,
                )
            except BaseException:
                stop_reason = self.lifecycle.execution_stop()
                if not stop_reason:
                    raise
                directive = LoopDirective("stop", stop_reason)
        if directive.kind == "stop":
            return self.lifecycle.finish_stopped(
                directive.detail,
                attempt_started_at=attempt_started_at,
            )
        raise RuntimeError("Agent loop exited without a terminal directive")

    def _prepare_prompt(self, loop_state, tool_surface):
        agent = self.agent
        if agent.prompt.refresh_project_instructions():
            self._reset_context(loop_state, "project_instructions_changed")
        if loop_state.prompt_snapshot is None:
            prompt = agent.prompt.build_for_run(
                tool_surface=tool_surface,
                force_compaction=loop_state.force_compaction,
            )
            loop_state.force_compaction = False
            loop_state.prompt_snapshot = prompt
        else:
            prompt = loop_state.prompt_snapshot
        return prompt

    def _reset_context(self, loop_state, reason=None, *, force_compaction=False, **details):
        self.agent.model_client.reset_action_session()
        loop_state.prompt_snapshot = None
        loop_state.force_compaction = bool(force_compaction)
        if reason is not None:
            self.agent.emit_event("provider_session_reset", {"reason": reason, **details})

    def _request_turn(
        self,
        loop_state,
        prompt,
        tool_surface,
    ):
        agent = self.agent
        input_tokens = agent.model_client.estimate_action_input_tokens(
            prompt.messages,
            system_prompt=prompt.system_prompt,
            action_tools=tool_surface.action_tools,
            token_counter=agent.prompt.count_tokens,
        )
        if loop_state.overflow_recovery_attempted and input_tokens >= loop_state.last_request_input_tokens:
            raise ProviderContextOverflow(
                "context overflow recovery did not reduce the full request "
                f"({loop_state.last_request_input_tokens} -> {input_tokens} estimated input tokens); "
                "check the model context/output limits or reduce required context"
            )
        loop_state.last_request_input_tokens = input_tokens
        agent.emit_event("model_requested")
        turn = agent.model_client.complete_turn(
            prompt.messages,
            agent.config.max_output_tokens,
            system_prompt=prompt.system_prompt,
            action_tools=tool_surface.action_tools,
            execution_context=agent.run.execution_context,
        )
        return turn

    def _provider_high_watermark(self):
        return self.agent.effective_input_limit_tokens

    def _continue_provider(
        self,
        loop_state,
        prompt,
        tool_surface,
        provider_results,
    ):
        agent = self.agent
        provider_results = tuple(str(result) for result in provider_results)
        projected_tokens = agent.model_client.projected_context_tokens(
            provider_results,
            system_prompt=prompt.system_prompt,
            action_tools=tool_surface.action_tools,
            token_counter=agent.prompt.count_tokens,
        )
        if projected_tokens >= self._provider_high_watermark():
            threshold_tokens = self._provider_high_watermark()
            self._reset_context(
                loop_state, "context_high_watermark",
                input_tokens=agent.model_client.last_completion_metadata.get("input_tokens"),
                projected_input_tokens=projected_tokens,
                threshold_tokens=threshold_tokens,
            )
            return
        agent.model_client.record_action_results(provider_results)

    def _recover_context_overflow(self, loop_state):
        if loop_state.overflow_recovery_attempted:
            return False
        loop_state.overflow_recovery_attempted = True
        self._reset_context(
            loop_state, "context_overflow_retry",
            force_compaction=True,
        )
        return True

    def _handle_tool_turn(self, loop_state, action, prompt, tool_surface):
        agent = self.agent
        calls = action.tool_calls
        if not calls:
            raise RuntimeError("tool turn is missing tool calls")
        loop_state.invalid_output_count = 0
        outcomes = []
        failure_result_indexes = []
        for call in calls:
            stop_reason = self.lifecycle.execution_stop()
            if stop_reason:
                agent.tools.close_unstarted_calls(stop_reason)
                return LoopDirective("stop", stop_reason)
            outcome = agent.tools.execute_call(call, tool_surface)
            if outcome.status != "success":
                failure = agent.run.projection.failure
                if failure is None:
                    raise RuntimeError("Tool Result did not update failure state")
                if failure.count >= 4:
                    agent.tools.close_unstarted_calls("repeated_failure")
                    return LoopDirective("stop", "repeated_failure")
                failure_result_indexes.append((len(outcomes), failure.signature))
            outcomes.append(outcome)
        guidance_index = None
        failure = agent.run.projection.failure
        if failure is not None and failure.category == "tool" and failure.count == 3:
            guidance_index = next(
                (
                    index
                    for index, signature in reversed(failure_result_indexes)
                    if signature == failure.signature
                ),
                None,
            )
        guidance = guidance_for_failure(failure) if guidance_index is not None else ""
        results = tuple(
            outcome.render_for_model(
                retry_instruction=guidance if index == guidance_index else ""
            )
            for index, outcome in enumerate(outcomes)
        )
        self._continue_provider(
            loop_state,
            prompt,
            tool_surface,
            results,
        )
        agent.dependencies.run_store.checkpoint_if_due(agent.run.run_log)
        return LoopDirective("continue")

    def _handle_model_failure(self, loop_state, action, prompt, tool_surface):
        self.agent.run.run_log.append_model_failure(
            action.kind,
            self.agent.redact_text(action.content),
            self.agent.redact_text(action.content),
            getattr(self.agent.model_client, "last_completion_metadata", {}) or {},
        )
        failure = self.agent.run.projection.failure
        if failure is None:
            raise RuntimeError("Model Failure did not update failure state")
        if action.kind == "service_failed":
            return LoopDirective("stop", "provider_failure")
        loop_state.invalid_output_count += 1
        if failure.count >= 4:
            return LoopDirective("stop", "repeated_failure")
        guidance = guidance_for_failure(failure)
        if not guidance:
            raise RuntimeError("model failure has no safe correction")
        self._continue_provider(
            loop_state,
            prompt,
            tool_surface,
            (guidance,),
        )
        self.agent.dependencies.run_store.checkpoint_if_due(
            self.agent.run.run_log
        )
        if loop_state.invalid_output_count >= 8:
            return LoopDirective("stop", "model_failure_limit")
        return LoopDirective("continue")

    def _handle_final_action(self, loop_state, turn):
        loop_state.invalid_output_count = 0
        return LoopDirective("complete", turn)
