"""Pico composition root and small public runtime facade."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import security as securitylib
from .artifacts import ArtifactStore
from .command_runner import CommandRunner
from .delivery import build_stopped_final_diff_descriptor
from .mutations import WorkspaceMutationService
from .prompt_builder import PromptBuilder
from .repo_map import RepoMap
from .run_lifecycle import load_resumable_run, reload_current_run
from .run_projection import RunOutcome
from .run_store import RunStore
from .runtime_config import PicoConfig
from .runtime_dependencies import RuntimeDependencies
from .runtime_session import RuntimeSession
from .runtime_state import ActiveRunState
from .session_store import SessionStore
from .tool_runtime import ToolRuntime
from .verification import run_verification

__all__ = ["Pico", "PicoConfig", "RunOutcome", "SessionStore"]


class Pico:
    """Coordinate model, state, prompt, tools, and long-lived dependencies."""

    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        *,
        config: PicoConfig | None = None,
        session=None,
        run_store=None,
        command_runner=None,
        command_runner_factory=None,
        subagent_model_client_factory=None,
        parent_execution_context=None,
    ):
        self.model_client = model_client
        self.config = PicoConfig.build(config)
        self.workspace = workspace
        self.run = ActiveRunState()
        self.session = RuntimeSession(
            session_store,
            self.workspace.root,
            session=session,
        )

        effective_run_store = run_store or RunStore(
            self.workspace.root / ".pico" / "runs"
        )
        artifacts = ArtifactStore(effective_run_store, self.redact_text)
        mutations = WorkspaceMutationService(self.workspace.root)

        if command_runner_factory is None:
            command_runner_factory = CommandRunner
        effective_command_runner = (
            command_runner or command_runner_factory(self.workspace.root)
        )
        self.dependencies = RuntimeDependencies(
            run_store=effective_run_store,
            artifacts=artifacts,
            mutations=mutations,
            command_runner=effective_command_runner,
            command_runner_factory=command_runner_factory,
            repo_map=RepoMap(self.workspace.root),
            parent_execution_context=parent_execution_context,
        )
        if subagent_model_client_factory is not None:
            from .subagents import SubagentRunner

            self.dependencies.subagents = SubagentRunner(
                self,
                subagent_model_client_factory,
            )

        self.tools = ToolRuntime(self)
        self.prompt = PromptBuilder(self)
        self.session.save()
        load_resumable_run(self)

    def redact_text(self, text):
        return securitylib.redact_text(
            text,
            secret_env_names=self.config.secret_env_names,
        )

    def redact_value(self, value, key=None):
        return securitylib.redact_value(
            value,
            key=key,
            secret_env_names=self.config.secret_env_names,
        )

    def emit_event(self, event_type, payload=None):
        task_state = self.run.task
        run_log = self.run.run_log
        if task_state is None or run_log is None:
            raise RuntimeError("Run event requires an active TaskState and RunLog")
        if self.run.projection.run_id != run_log.run_id:
            raise RuntimeError("active TaskState and RunLog belong to different Runs")
        payload = self.redact_value(payload or {})
        entry = run_log.append(event_type, payload)
        return self.apply_run_event(entry)

    def apply_run_event(self, entry):
        if self.run.task is None or self.run.projection.run_id != entry.run_id:
            raise RuntimeError("Run event does not belong to the active projection")
        self.run.projection.apply_event(entry)
        return entry

    def run_verification(self, started_workspace_mutation_sequence):
        return run_verification(self, started_workspace_mutation_sequence)

    def read_run_events(self, run_id):
        return self.dependencies.run_store.read_events(run_id)

    def ask(self, user_message) -> RunOutcome:
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    @staticmethod
    def new_task_id():
        return (
            "task_"
            + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )

    @staticmethod
    def new_run_id():
        return (
            "run_"
            + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6]
        )

    def reset(self):
        if self.run.reload_required:
            reload_current_run(self)
        execution = self.run.execution_context
        if execution is not None:
            execution.request_stop("user_reset")
            return
        run_log = self.run.run_log
        try:
            if run_log is not None and not self.run.projection.terminal:
                for _outcome, event in run_log.reconcile_interrupted(self):
                    self.apply_run_event(event)
                self.apply_run_event(
                    run_log.append_stopped(
                        "Session reset by user.",
                        "user_reset",
                        build_stopped_final_diff_descriptor(self),
                    )
                )
        except BaseException:
            self.run.reload_required = True
            reload_current_run(self)
            raise
        self.session.reset()
        self.run = ActiveRunState()
        self.model_client.reset_action_session()

    def cancel_current_run(self, reason="user_cancelled"):
        if self.run.execution_context is None:
            return False
        self.run.execution_context.request_stop(reason)
        return True
