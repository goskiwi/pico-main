"""Pico composition root and small public runtime facade."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import security as securitylib
from .artifacts import ArtifactStore
from .command_runner import CommandRunner
from .config import PicoConfig
from .contracts import AssistantTurn
from .mutations import WorkspaceMutationService
from .prompt_builder import PromptBuilder
from .run_lifecycle import RunLifecycle, load_resumable_run
from .run_projection import RunOutcome
from .runtime_dependencies import RuntimeDependencies
from .runtime_state import ActiveRunState
from .session_store import Session, SessionStore
from .tool_runtime import ToolRuntime

__all__ = ["Pico", "PicoConfig", "RunOutcome", "SessionStore"]


class Pico:
    """Coordinate model, state, prompt, tools, and long-lived dependencies."""

    def __init__(
        self,
        model_client,
        workspace,
        session: Session,
        *,
        config: PicoConfig | None = None,
        trace=None,
        command_runner=None,
        approval_handler=None,
    ):
        if session.workspace_root != workspace.root.resolve():
            raise ValueError("session belongs to another workspace")
        self.model_client = model_client
        self.config = config if config is not None else PicoConfig()
        model_context_window = getattr(model_client, "context_window_tokens", None)
        if model_context_window is not None:
            model_context_window = int(model_context_window)
            if model_context_window < 1:
                raise ValueError("model context window must be positive")
        model_input_limit = getattr(model_client, "input_limit_tokens", None)
        if model_input_limit is not None and int(model_input_limit) < 1:
            raise ValueError("model input limit must be positive")
        available_context = self.effective_input_limit_tokens
        if available_context < 1:
            raise ValueError("model context window must exceed max_output_tokens")
        if self.config.recent_history_tokens > available_context:
            raise ValueError("recent history must fit the effective context limit")
        self.workspace = workspace
        self.run = ActiveRunState()
        self.session = session

        effective_run_store = session.store.runs(session.id, trace=trace)
        artifacts = ArtifactStore(effective_run_store, self.redact_text)
        mutations = WorkspaceMutationService(self.workspace.root)

        effective_command_runner = command_runner or CommandRunner(self.workspace.root)
        self.dependencies = RuntimeDependencies(
            run_store=effective_run_store,
            artifacts=artifacts,
            mutations=mutations,
            command_runner=effective_command_runner,
            approval_handler=approval_handler,
        )

        self.tools = ToolRuntime(self)
        self.prompt = PromptBuilder(self)
        load_resumable_run(self)

    @property
    def effective_context_limit_tokens(self):
        """Return the smaller Runtime policy limit and Provider capability."""

        model_limit = getattr(self.model_client, "context_window_tokens", None)
        policy_limit = self.config.context_limit_tokens
        if model_limit is None and policy_limit is None:
            raise ValueError(
                "context window is unknown; configure the model capability or "
                "PicoConfig.context_limit_tokens"
            )
        if model_limit is None:
            return int(policy_limit)
        if policy_limit is None:
            return int(model_limit)
        return min(int(policy_limit), int(model_limit))

    @property
    def effective_input_limit_tokens(self):
        """Return the strictest Provider input limit after output reservation."""

        combined_limit = (
            self.effective_context_limit_tokens
            - self.config.max_output_tokens
        )
        provider_limit = getattr(self.model_client, "input_limit_tokens", None)
        return (
            combined_limit
            if provider_limit is None
            else min(combined_limit, int(provider_limit))
        )

    def redact_text(self, text):
        return securitylib.redact_text(text)

    def redact_turn(self, turn):
        if not isinstance(turn, AssistantTurn):
            raise TypeError("turn redaction requires an AssistantTurn")
        return AssistantTurn.from_dict(
            securitylib.redact_facts(turn.to_dict(), self.redact_text)
        )

    def emit_event(self, event_type, payload=None):
        task_state = self.run.projection
        run_log = self.run.run_log
        if task_state.contract is None or run_log is None:
            raise RuntimeError("Run event requires an active contract and RunLog")
        if self.run.projection.run_id != run_log.run_id:
            raise RuntimeError("active Projection and RunLog belong to different Runs")
        payload = securitylib.redact_facts(payload or {}, self.redact_text)
        entry = run_log.append(event_type, payload)
        return entry

    def read_run_events(self, run_id):
        return self.dependencies.run_store.read_events(run_id)

    def run_status(self):
        run_log = self.run.run_log
        if run_log is None:
            return {"run": None, "context": None}
        return {
            "run": run_log.projection.summary(),
            "context": run_log.context_state.to_dict(),
        }

    def ask(self, user_message) -> RunOutcome:
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    @staticmethod
    def new_run_id():
        return (
            "run_"
            + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )

    def reset(self):
        execution = self.run.execution_context
        if execution is not None:
            execution.request_stop("user_reset")
            return
        run_log = self.run.run_log
        if run_log is not None and not self.run.projection.terminal:
            RunLifecycle(self).reset_dormant()
            return
        self.session.set_active_run("")
        self.run = ActiveRunState()
        self.model_client.reset_action_session()

    def cancel_current_run(self, reason="user_cancelled"):
        if self.run.execution_context is None:
            return False
        self.run.execution_context.request_stop(reason)
        return True
