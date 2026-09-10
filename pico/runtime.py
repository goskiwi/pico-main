"""Pico's composition root and small public facade."""

from __future__ import annotations

from dataclasses import replace

from . import security as securitylib
from .artifacts import ArtifactStore
from .command_runner import CommandRunner
from .config import PicoConfig
from .context_manager import ContextManager
from .memory import MemoryStore
from .model_usage import MeteredClient, ModelUsage
from .mutations import WorkspaceMutationService
from .outcome import RunOutcome
from .session_store import Session, SessionStore
from .tool_runtime import ToolRuntime
from .trace import TracePrinter
from .verification import detect_verification_command

__all__ = ["Pico", "PicoConfig", "RunOutcome", "SessionStore"]


class Pico:
    """Own one Session and the services used to advance it."""

    def __init__(self):
        raise TypeError("Use Pico.create() or Pico.resume()")

    @classmethod
    def create(cls, model_client, workspace, *, session_store, session_id=None, **options):
        session = session_store.create(workspace.root, session_id=session_id)
        return cls._assemble(model_client, workspace, session, **options)

    @classmethod
    def resume(cls, model_client, workspace, *, session, **options):
        if session.workspace_root != str(workspace.root.resolve()):
            raise ValueError("session belongs to another workspace")
        runtime = cls._assemble(model_client, workspace, session, **options)
        recovered = runtime.session.recover()
        if recovered:
            runtime.session.append_feedback(
                "Recovered interrupted operations without replaying them. "
                "Inspect current state before proposing another mutation."
            )
            runtime.session.save()
        return runtime

    @classmethod
    def _assemble(cls, model_client, workspace, session, **options):
        runtime = cls.__new__(cls)
        runtime._initialize(model_client, workspace, session, **options)
        return runtime

    def _initialize(
        self,
        model_client,
        workspace,
        session: Session,
        *,
        config: PicoConfig | None = None,
        trace=None,
        command_runner=None,
        command_runner_factory=None,
        approval_handler=None,
        parent_execution_context=None,
    ):
        self.usage = ModelUsage()
        self.model_client = MeteredClient(model_client, self.usage)
        self.workspace = workspace
        self.session = session
        self.config = config or PicoConfig()
        if not self.config.verification_command.strip():
            self.config = replace(
                self.config,
                verification_command=detect_verification_command(workspace.root),
            )
        self.trace = trace if trace is not None else TracePrinter(None)
        self.approval_handler = approval_handler
        factory = command_runner_factory or CommandRunner
        self.command_runner = command_runner or factory(workspace.root)
        self.mutations = WorkspaceMutationService(workspace.root)
        self.artifacts = ArtifactStore(session.store, self.redact_text)
        if self.trace is not None and hasattr(self.trace, "bind"):
            self.trace.bind(
                session.store.directory(session.id) / "trace.jsonl",
                self.redact_facts,
            )
        self.memory = MemoryStore(workspace.root / ".pico" / "memory.json", self.redact_text)
        self.current_memories = []
        self.parent_execution_context = parent_execution_context
        self.execution_context = None
        self.tools = ToolRuntime(self)
        self.context = ContextManager(self)

    def redact_text(self, text):
        return securitylib.redact_text(text)

    def redact_facts(self, value):
        return securitylib.redact_facts(value, self.redact_text)

    def ask(self, user_message) -> RunOutcome:
        from .agent_loop import AgentLoop

        if self.execution_context is not None:
            raise RuntimeError("a Session may have only one active request")
        self.session = self.session.store.load(self.session.id, self.workspace.root)
        self.session.recover()
        self.session.save()
        self.tools.read_versions.clear()
        try:
            return AgentLoop(self).run(user_message)
        finally:
            self.execution_context = None

    def cancel_current_run(self, reason="user_cancelled"):
        if self.execution_context is None:
            return False
        self.execution_context.request_stop(reason)
        return True

    def reset(self):
        if self.execution_context is not None:
            self.execution_context.request_stop("user_reset")
            return
        self.session.run = {"status": "reset"}
        self.session.save()
        replacement = self.session.store.create(self.workspace.root)
        self.session = replacement
        self.artifacts = ArtifactStore(replacement.store, self.redact_text)
        if self.trace is not None and hasattr(self.trace, "bind"):
            self.trace.bind(
                replacement.store.directory(replacement.id) / "trace.jsonl",
                self.redact_facts,
            )
        self.current_memories = []
        self.tools = ToolRuntime(self)

    def emit_trace(self, kind, **payload):
        if self.trace is None:
            return
        event = getattr(self.trace, "event", None)
        try:
            if callable(event):
                event(kind, payload)
            elif callable(self.trace):
                self.trace(kind, payload)
        except (OSError, ValueError):
            # Diagnostic output never controls durable Session state.
            pass
