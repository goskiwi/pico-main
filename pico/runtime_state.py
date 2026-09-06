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
    request_tool_start: int = 0

    @property
    def evidence(self):
        return self.projection.evidence

    @property
    def metrics(self):
        return self.projection.metrics

    @property
    def resumable(self):
        """Whether this unfinished Run is dormant and safe to resume."""

        return bool(
            self.projection.contract is not None
            and self.run_log is not None
            and not self.projection.terminal
            and self.execution_context is None
        )
