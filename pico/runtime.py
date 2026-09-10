"""Pico composition root and small public runtime facade."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from . import security as securitylib
from .artifacts import ArtifactStore
from .command_runner import CommandRunner
from .config import PicoConfig
from .mutations import WorkspaceMutationService
from .prompt_builder import PromptBuilder
from .run_lifecycle import RunLifecycle, load_resumable_run
from .run_projection import RunOutcome
from .runtime_dependencies import RuntimeDependencies
from .runtime_state import ActiveRunState
from .session_store import Session, SessionStore
from .tool_runtime import ToolRuntime
from .verification import detect_verification_command, run_verification
from .workspace import clip

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
        if not self.config.verification_command.strip():
            self.config = replace(
                self.config,
                verification_command=detect_verification_command(workspace.root),
            )
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

    def redact_text(self, text):
        return securitylib.redact_text(text)

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

    def append_model_instruction(self, instruction, *, evidence=""):
        run_log = self.run.run_log
        if run_log is None:
            raise RuntimeError("Runtime instruction requires an active RunLog")
        instruction = self.redact_text(str(instruction))
        evidence = self.redact_text(str(evidence))
        descriptor = {}
        if evidence:
            descriptor = self.dependencies.artifacts.write_tool_output(
                self.run.projection.run_id,
                f"runtime_instruction_{len(run_log.events) + 1}",
                evidence,
            )
            evidence = (
                clip(evidence, 2000)
                + "\n[Full untrusted evidence: artifact_id="
                + descriptor["artifact_id"]
                + ". Use read_artifact to inspect it.]"
            )
        return run_log.append_model_instruction(
            instruction,
            evidence=evidence,
            evidence_artifact_id=str(descriptor.get("artifact_id", "")),
        )

    def run_verification(self, started_workspace_mutation_sequence, policy):
        return run_verification(
            self,
            started_workspace_mutation_sequence,
            policy,
        )

    def read_run_events(self, run_id):
        return self.dependencies.run_store.read_events(run_id)

    def ask(self, user_message) -> RunOutcome:
        from .agent_loop import AgentLoop

        # Require a fresh read before editing in each new user request.
        self.tools.read_versions.clear()
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
