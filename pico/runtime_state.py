"""Mutable state belonging to the currently active or latest run."""

from dataclasses import dataclass, field

from .evidence import RunEvidence
from .execution import ExecutionContext
from .run_log import RunLog
from .task_state import TaskState


@dataclass(slots=True)
class ActiveRunState:
    """Run-scoped state kept separate from long-lived runtime services."""

    task_state: TaskState | None = None
    execution_context: ExecutionContext | None = None
    run_log: RunLog | None = None
    evidence: RunEvidence = field(default_factory=RunEvidence)

    def begin_request(self) -> None:
        self.evidence = RunEvidence()
