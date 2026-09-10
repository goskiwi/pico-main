"""Mutable state belonging to the currently active or latest run."""

from dataclasses import dataclass

from .execution import ExecutionContext
from .run_log import RunLog


@dataclass(slots=True)
class ActiveRunState:
    """Internal Run-scoped state; an active Projection belongs to its RunLog."""

    run_log: RunLog | None = None
    execution_context: ExecutionContext | None = None

    @property
    def projection(self):
        if self.run_log is None:
            raise RuntimeError("Run state has no RunLog")
        return self.run_log.projection

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
