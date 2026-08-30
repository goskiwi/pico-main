"""Mutable state belonging to the currently active or latest run."""

from dataclasses import dataclass, field

from .execution import ExecutionContext
from .run_log import RunLog
from .run_projection import RunProjection


@dataclass(slots=True)
class ActiveRunState:
    """Run-scoped state kept separate from long-lived runtime dependencies."""

    projection: RunProjection = field(default_factory=RunProjection)
    execution_context: ExecutionContext | None = None
    run_log: RunLog | None = None

    @property
    def task(self):
        return self.projection.task

    @property
    def evidence(self):
        return self.projection.evidence

    @property
    def metrics(self):
        return self.projection.metrics
