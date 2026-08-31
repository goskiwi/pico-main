"""Shared execution deadlines and cancellation."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field


class ExecutionDeadlineExceeded(TimeoutError):
    pass


class ExecutionCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionBudget:
    """One immutable resource contract for a command execution attempt."""

    deadline: float
    max_output_bytes: int

    def __post_init__(self):
        if int(self.max_output_bytes) < 1024:
            raise ValueError("execution max_output_bytes must be at least 1024")


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
    deadline: float
    token: CancellationToken

    @classmethod
    def root(cls, *, max_seconds, token=None, deadline=None):
        return cls(
            execution_id="exec_" + uuid.uuid4().hex,
            deadline=(
                float(deadline)
                if deadline is not None
                else time.monotonic() + float(max_seconds)
            ),
            token=token or CancellationToken(),
        )

    @classmethod
    def standalone(cls, *, max_seconds):
        return cls.root(max_seconds=max_seconds)

    def child(self):
        return ExecutionContext(
            execution_id="exec_" + uuid.uuid4().hex,
            deadline=self.deadline,
            token=self.token,
        )

    def remaining_seconds(self):
        return max(0.0, self.deadline - time.monotonic())

    def bounded_timeout(self, requested=None):
        self.check_active()
        remaining = self.remaining_seconds()
        if remaining <= 0:
            raise ExecutionDeadlineExceeded("execution deadline exceeded")
        return remaining if requested is None else min(float(requested), remaining)

    def check_active(self):
        if self.token.requested:
            raise ExecutionCancelled(self.token.reason or "execution cancelled")
        if self.remaining_seconds() <= 0:
            raise ExecutionDeadlineExceeded("execution deadline exceeded")

    def request_stop(self, reason="user_cancelled"):
        self.token.request(reason)
