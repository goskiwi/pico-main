"""Pico composition root and small public runtime facade."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import security as securitylib
from .artifacts import ArtifactStore
from .hooks import HookRunner
from .mutations import WorkspaceMutationService
from .project_memory import ProjectMemoryStore
from .projections import build_run_report
from .repo_map import RepoMap
from .run_journal import JournalEntry, RunJournal, replay_entries
from .run_store import RunStore
from .runtime_config import PicoConfig
from .runtime_prompt import RuntimePrompt
from .runtime_recovery import RESUME_NONE, RESUME_READY, RuntimeRecovery
from .runtime_services import RuntimeServices
from .runtime_session import RuntimeSession
from .runtime_state import ActiveRunState
from .runtime_tools import RuntimeTools
from .sandbox import DockerSandbox, DockerSandboxConfig
from .session_store import SessionStore
from .task_state import TaskState
from .verification import discover_verification_command, run_verification
from .workspace_tracker import WorkspaceTracker

__all__ = ["Pico", "PicoConfig", "SessionStore"]


class Pico:
    """Coordinate model, state, prompt, tools, and long-lived services."""

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
        hooks=None,
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
        if self.config.verification_command is None:
            self.config = PicoConfig.build(
                self.config,
                verification_command=discover_verification_command(
                    self.workspace.root
                ),
            )

        self.services = RuntimeServices(
            run_store=effective_run_store,
            artifacts=artifacts,
            project_memory=project_memory,
            mutations=mutations,
            sandbox=effective_sandbox,
            sandbox_factory=sandbox_factory,
            hooks=HookRunner(hooks),
            repo_map=RepoMap(self.workspace.root),
            parent_cancellation_token=parent_cancellation_token,
        )
        if subagent_model_client_factory is not None:
            from .subagents import SubagentManager

            self.services.subagents = SubagentManager(
                self,
                subagent_model_client_factory,
                max_workers=self.config.subagent_max_workers,
            )

        self.tools = RuntimeTools(self)
        self.recovery = RuntimeRecovery(self)
        self.prompt = RuntimePrompt(self)
        self.recovery.evaluate()
        self.session.save()

    def feature_enabled(self, name):
        return bool(self.config.feature_flags.get(str(name), False))

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(
            secret_env_names=self.config.secret_env_names
        )

    def redact_text(self, text):
        return securitylib.redact_text(
            text,
            secret_env_names=self.config.secret_env_names,
        )

    def redact_artifact(self, value, key=None):
        return securitylib.redact_artifact(
            value,
            key=key,
            secret_env_names=self.config.secret_env_names,
        )

    def shell_env(self):
        return securitylib.shell_env(
            allowlist=self.config.shell_env_allowlist,
            root=self.workspace.root,
        )

    def emit_event(self, task_state, event_type, payload=None):
        payload = self.redact_artifact(payload or {})
        if task_state is None:
            return JournalEntry(
                entry_id="manual:entry:000001",
                sequence=1,
                run_id="manual",
                task_id="manual",
                session_id=self.session.data["id"],
                kind=event_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        run_id = task_state.run_id if task_state is not None else "manual"
        task_id = task_state.task_id if task_state is not None else "manual"
        if self.run.journal is not None and self.run.journal.run_id == run_id:
            entry = self.run.journal.append(event_type, payload)
        else:
            entry = self.services.run_store.append_entry(
                run_id,
                task_id,
                self.session.data["id"],
                event_type,
                payload,
            )
        recovery = getattr(self, "recovery", None)
        projection = recovery.state.get("projection") if recovery is not None else None
        if (
            projection is not None
            and projection.run_id == entry.run_id
            and entry.sequence > projection.last_cursor.sequence
        ):
            projection.apply(entry)
        return entry

    def run_verification(self, workspace_fingerprint):
        return run_verification(self, workspace_fingerprint)

    def ask(self, user_message):
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

    def build_report(self, task_state):
        return build_run_report(
            entries=self.services.run_store.read_entries(task_state.run_id),
            prompt_metadata=self.run.last_prompt_metadata,
            project_memory_count=self.services.project_memory.count(),
            redacted_env=self.detected_secret_env_summary(),
        )

    def reset(self):
        journal = self.run.journal
        task_state = self.run.task_state
        recovery_state = dict(self.recovery.state)
        if journal is None and recovery_state.get("status") == RESUME_READY:
            entries = tuple(recovery_state.get("entries", ()))
            if entries:
                first = entries[0]
                journal = RunJournal(
                    first.run_id,
                    first.task_id,
                    first.session_id,
                    self.services.run_store,
                    entries,
                )
                projection = recovery_state.get("projection") or replay_entries(entries)
                task_state = TaskState.from_dict(projection.task_state())
        if journal is not None and not replay_entries(journal.entries).terminal:
            self.run.journal = journal
            self.run.task_state = task_state
            journal.reconcile_interrupted(self)
            journal.append_stopped(
                "Session reset by user.",
                "user_reset",
            )
        if self.run.execution is not None:
            self.run.execution.request_stop("user_reset")
        self.session.reset()
        self.run = ActiveRunState()
        self.recovery.state = {
            "status": RESUME_NONE,
            "active_run_id": "",
            "projection": None,
            "entries": (),
        }
        self.model_client.reset_action_session()

    def cancel_current_run(self, reason="user_cancelled"):
        if self.run.execution is None:
            return False
        self.run.execution.request_stop(reason)
        return True
