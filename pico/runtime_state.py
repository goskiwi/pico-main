"""Mutable state belonging to the currently active or latest run."""

from dataclasses import dataclass, field

from .execution import ExecutionContext
from .run_log import RunLog
from .run_projection import RunProjection


@dataclass(slots=True)
class ActiveRunState:
    """Internal Run-scoped state; an active Projection belongs to its RunLog."""

    run_log: RunLog | None = None
    execution_context: ExecutionContext | None = None
    request_tool_start: int = 0
    _empty_projection: RunProjection = field(
        default_factory=RunProjection,
        repr=False,
    )

    @property
    def projection(self):
        return (
            self.run_log.projection
            if self.run_log is not None
            else self._empty_projection
        )

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
            self.run_log is not None
            and self.projection.contract is not None
            and not self.projection.terminal
            and self.execution_context is None
        )
