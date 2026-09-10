"""Small application wrapper for one verified coding task."""

from dataclasses import dataclass
from pathlib import Path

from pico import Pico, PicoConfig, RunOutcome, SessionStore, Workspace


@dataclass(frozen=True)
class CodingResult:
    outcome: RunOutcome
    session_path: str


class CodingWorkflow:
    """Run Pico and return its report without committing or publishing changes."""

    def __init__(self, model_client, *, config=None, approval_handler=None):
        self.model_client = model_client
        self.config = config or PicoConfig()
        self.approval_handler = approval_handler

    def run(self, repository_root, request):
        root = Path(repository_root).resolve()
        workspace = Workspace.build(root, repo_root_override=root)
        store = SessionStore(root / ".pico" / "sessions")
        agent = Pico.create(
            self.model_client,
            workspace,
            session_store=store,
            config=self.config,
            approval_handler=self.approval_handler,
        )
        outcome = agent.ask(request)
        return CodingResult(outcome, str(agent.session.path))


__all__ = ["CodingResult", "CodingWorkflow"]
