"""Shared execution deadlines, cancellation, and terminal lifecycle facts."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

EXECUTION_STATES = frozenset(
    {
        "requested",
        "admitted",
        "starting",
        "running",
        "stop_requested",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "killed",
    }
)
TERMINAL_EXECUTION_STATES = frozenset(
    {"completed", "failed", "timed_out", "cancelled", "killed"}
)
CLEANUP_STATES = frozenset(
    {"not_required", "pending", "completed", "failed"}
)


class ExecutionDeadlineExceeded(TimeoutError):
    pass


class ExecutionCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionBudget:
    """One immutable resource contract for a sandbox execution attempt."""

    deadline: float
    max_output_bytes: int
    max_processes: int
    memory: str

    def __post_init__(self):
        if int(self.max_output_bytes) < 1024:
            raise ValueError("execution max_output_bytes must be at least 1024")
        if int(self.max_processes) < 1:
            raise ValueError("execution max_processes must be positive")
        if not str(self.memory).strip():
            raise ValueError("execution memory budget is required")

    def to_dict(self):
        return {
            "deadline_monotonic": float(self.deadline),
            "max_output_bytes": int(self.max_output_bytes),
            "max_processes": int(self.max_processes),
            "memory": self.memory,
        }


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)
    _reason: str = ""
    parent: CancellationToken | None = None

    @property
    def requested(self):
        return self._event.is_set() or bool(self.parent and self.parent.requested)

    @property
    def reason(self):
        if self._event.is_set():
            return self._reason
        if self.parent and self.parent.requested:
            return self.parent.reason
        return ""

    def request(self, reason="user_cancelled"):
        if not self._event.is_set():
            self._reason = str(reason or "user_cancelled")
            self._event.set()

    def child(self):
        """Return a token cancelled by its parent but independently stoppable."""
        return CancellationToken(parent=self)


@dataclass
class ExecutionContext:
    execution_id: str
    run_id: str
    task_id: str
    owner: str
    deadline: float
    token: CancellationToken
    tool_call_id: str = ""
    parent_execution_id: str = ""
    state: str = "requested"
    cleanup_state: str = "not_required"
    stop_reason: str = ""

    @classmethod
    def root(
        cls, *, run_id, task_id, owner, max_seconds, token=None, deadline=None
    ):
        return cls(
            execution_id="exec_" + uuid.uuid4().hex,
            run_id=str(run_id),
            task_id=str(task_id),
            owner=str(owner),
            deadline=(
                float(deadline)
                if deadline is not None
                else time.monotonic() + float(max_seconds)
            ),
            token=token or CancellationToken(),
        )

    @classmethod
    def standalone(cls, *, owner, max_seconds, tool_call_id=""):
        context = cls.root(
            run_id="",
            task_id="",
            owner=owner,
            max_seconds=max_seconds,
        )
        context.tool_call_id = str(tool_call_id or "")
        return context

    def child(self, *, owner, tool_call_id=""):
        return ExecutionContext(
            execution_id="exec_" + uuid.uuid4().hex,
            run_id=self.run_id,
            task_id=self.task_id,
            owner=str(owner),
            deadline=self.deadline,
            token=self.token,
            tool_call_id=str(tool_call_id or ""),
            parent_execution_id=self.execution_id,
        )

    def remaining_seconds(self):
        return max(0.0, self.deadline - time.monotonic())

    def bounded_timeout(self, requested=None):
        self.check_active()
        remaining = self.remaining_seconds()
        if remaining <= 0:
            self.transition("timed_out", stop_reason="deadline_exceeded")
            raise ExecutionDeadlineExceeded("execution deadline exceeded")
        return remaining if requested is None else min(float(requested), remaining)

    def check_active(self):
        if self.token.requested:
            self.transition("stop_requested", stop_reason=self.token.reason)
            raise ExecutionCancelled(self.token.reason or "execution cancelled")
        if self.remaining_seconds() <= 0:
            self.transition("timed_out", stop_reason="deadline_exceeded")
            raise ExecutionDeadlineExceeded("execution deadline exceeded")

    def request_stop(self, reason="user_cancelled"):
        self.token.request(reason)
        if self.state not in TERMINAL_EXECUTION_STATES:
            self.transition("stop_requested", stop_reason=self.token.reason)

    def transition(self, state, *, cleanup_state=None, stop_reason=""):
        if state not in EXECUTION_STATES:
            raise ValueError(f"invalid execution state: {state}")
        if self.state in TERMINAL_EXECUTION_STATES and state != self.state:
            return self
        self.state = state
        if cleanup_state is not None:
            if cleanup_state not in CLEANUP_STATES:
                raise ValueError(f"invalid cleanup state: {cleanup_state}")
            self.cleanup_state = cleanup_state
        if stop_reason:
            self.stop_reason = str(stop_reason)
        return self

    def to_dict(self):
        return {
            "execution_id": self.execution_id,
            "parent_execution_id": self.parent_execution_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "tool_call_id": self.tool_call_id,
            "owner": self.owner,
            "state": self.state,
            "cleanup_state": self.cleanup_state,
            "stop_reason": self.stop_reason,
            "remaining_ms": int(self.remaining_seconds() * 1000),
            "cancellation_requested": self.token.requested,
        }
