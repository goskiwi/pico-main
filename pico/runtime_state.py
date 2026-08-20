"""Mutable state belonging to the currently active or latest run."""

from dataclasses import dataclass, field

from .evidence import EvidenceLedger
from .execution import ExecutionContext
from .run_journal import RunJournal
from .task_state import TaskState


@dataclass(slots=True)
class ActiveRunState:
    """Run-scoped state kept separate from long-lived runtime services."""

    task_state: TaskState | None = None
    execution: ExecutionContext | None = None
    journal: RunJournal | None = None
    evidence: EvidenceLedger = field(default_factory=EvidenceLedger)
    last_prompt_metadata: dict[str, object] = field(default_factory=dict)
    task_memory_selection: dict[str, object] | None = None

    def begin_request(self) -> None:
        self.task_memory_selection = None
        self.evidence = EvidenceLedger()
