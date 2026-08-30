"""Pico composition root and small public runtime facade."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import security as securitylib
from .artifacts import ArtifactStore
from .delivery import build_stopped_final_diff_descriptor
from .mutations import WorkspaceMutationService
from .project_memory import ProjectMemoryStore
from .prompt_builder import PromptBuilder
from .repo_map import RepoMap
from .run_log import RunLog
from .run_store import RunStore
from .runtime_config import PicoConfig
from .runtime_dependencies import RuntimeDependencies
from .runtime_recovery import RESUME_NONE, RESUME_READY, RuntimeRecovery
from .runtime_session import RuntimeSession
from .runtime_state import ActiveRunState
from .sandbox import DockerSandbox, DockerSandboxConfig
from .session_store import SessionStore
from .tool_runtime import ToolRuntime
from .verification import run_verification
from .workspace_tracker import WorkspaceTracker

__all__ = ["Pico", "PicoConfig", "SessionStore"]


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
        sandbox=None,
        project_memory_root=None,
        sandbox_factory=None,
        subagent_model_client_factory=None,
        parent_cancellation_token=None,
    ):
        self.model_client = model_client
        self.config = PicoConfig.build(config)
        self.workspace = WorkspaceTracker(workspace)
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
        memory_root = (
            Path(project_memory_root)
            if project_memory_root is not None
            else self.workspace.root / ".pico" / "memory"
        )
        project_memory = ProjectMemoryStore(memory_root)
        mutations = WorkspaceMutationService(self.workspace.root)

        if sandbox_factory is None:
            sandbox_config = DockerSandboxConfig(image=self.config.sandbox_image)
            sandbox_factory = lambda root: DockerSandbox(root, sandbox_config)
        effective_sandbox = sandbox or sandbox_factory(self.workspace.root)
        self.dependencies = RuntimeDependencies(
            run_store=effective_run_store,
            artifacts=artifacts,
            project_memory=project_memory,
            mutations=mutations,
            sandbox=effective_sandbox,
            sandbox_factory=sandbox_factory,
            repo_map=RepoMap(self.workspace.root),
            parent_cancellation_token=parent_cancellation_token,
        )
        if subagent_model_client_factory is not None:
            from .subagents import SubagentManager

            self.dependencies.subagents = SubagentManager(
                self,
                subagent_model_client_factory,
                max_workers=self.config.subagent_max_workers,
            )

        self.tools = ToolRuntime(self)
        self.recovery = RuntimeRecovery(self)
        self.prompt = PromptBuilder(self)
        self.session.save()
        self.recovery.evaluate()

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

    def shell_env(self):
        return securitylib.shell_env(
            allowlist=self.config.shell_env_allowlist,
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

    def ask(
        self,
        user_message,
        *,
        task_kind,
        requires_workspace_change,
        requires_verification,
    ):
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(
            user_message,
            task_kind=task_kind,
            requires_workspace_change=requires_workspace_change,
            requires_verification=requires_verification,
        )

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
        run_log = self.run.run_log
        projection = self.run.projection
        recovery_state = dict(self.recovery.state)
        if run_log is None and recovery_state.get("status") == RESUME_READY:
            events = tuple(recovery_state.get("events", ()))
            if events:
                first = events[0]
                run_log = RunLog(
                    first.run_id,
                    first.task_id,
                    first.session_id,
                    self.dependencies.run_store,
                    events,
                )
                projection = recovery_state.get("projection") or (
                    self.dependencies.run_store.replay(first.run_id)
                )
        if run_log is not None and not projection.terminal:
            self.run.run_log = run_log
            self.run.projection = projection
            for _outcome, event in run_log.reconcile_interrupted(self):
                self.apply_run_event(event)
            self.apply_run_event(
                run_log.append_stopped(
                    "Session reset by user.",
                    "user_reset",
                    build_stopped_final_diff_descriptor(self),
                )
            )
        if self.run.execution_context is not None:
            self.run.execution_context.request_stop("user_reset")
        self.session.reset()
        self.run = ActiveRunState()
        self.recovery.state = {
            "status": RESUME_NONE,
            "active_run_id": "",
            "projection": None,
            "events": (),
        }
        self.model_client.reset_action_session()

    def cancel_current_run(self, reason="user_cancelled"):
        if self.run.execution_context is None:
            return False
        self.run.execution_context.request_stop(reason)
        return True
