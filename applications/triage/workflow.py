"""Thin orchestration layer from TriageCase to Pico and TriageReport."""

from __future__ import annotations

import subprocess

from pico import Pico, PicoConfig, SessionStore, WorkspaceContext

from .case import TriageCase
from .prompt import build_triage_prompt
from .report import TriageReport, build_triage_report


class TriageWorkflow:
    def __init__(
        self,
        model_client,
        *,
        config: PicoConfig | None = None,
        subagent_model_client_factory=None,
        sandbox=None,
        sandbox_factory=None,
    ):
        self.model_client = model_client
        self.config = PicoConfig.build(config)
        self.subagent_model_client_factory = subagent_model_client_factory
        self.sandbox = sandbox
        self.sandbox_factory = sandbox_factory

    @staticmethod
    def _check_revision(case: TriageCase):
        def git(*args, required=True):
            result = subprocess.run(
                ["git", *args],
                cwd=case.repository_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                if required:
                    raise ValueError("Triage revision requires a Git repository")
                return ""
            return result.stdout.strip()

        current = git("rev-parse", "HEAD", required=bool(case.revision))
        if not current:
            return "working-tree"
        if not case.revision:
            return current
        expected = git("rev-parse", case.revision)
        if current != expected:
            raise ValueError(
                f"Triage repository is at {current}, expected {expected}"
            )
        return current

    def run(self, case: TriageCase) -> TriageReport:
        revision = self._check_revision(case)
        case = case.model_copy(update={"revision": revision})
        config = PicoConfig.build(
            self.config,
            verification_command=case.verifier,
        )
        agent = Pico(
            model_client=self.model_client,
            workspace=WorkspaceContext.build(
                case.repository_root,
                repo_root_override=case.repository_root,
            ),
            session_store=SessionStore(
                case.repository_root / ".pico" / "sessions"
            ),
            config=config,
            sandbox=self.sandbox,
            sandbox_factory=self.sandbox_factory,
            subagent_model_client_factory=self.subagent_model_client_factory,
        )
        answer = agent.ask(build_triage_prompt(case))
        events = agent.dependencies.run_store.read_events(agent.run.task_state.run_id)
        return build_triage_report(case, answer, events)
