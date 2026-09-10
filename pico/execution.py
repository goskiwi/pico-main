"""Shared execution deadlines and cancellation."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class ExecutionDeadlineExceeded(TimeoutError):
    pass


class ExecutionCancelled(RuntimeError):
    pass


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)
    _reason: str = ""

    @property
    def requested(self):
        return self._event.is_set()

    @property
    def reason(self):
        if self._event.is_set():
            return self._reason
        return ""

    def request(self, reason="user_cancelled"):
        if not self._event.is_set():
            self._reason = str(reason or "user_cancelled")
            self._event.set()

    def wait(self, timeout):
        """Wake immediately when cancellation is requested."""
        return self._event.wait(max(0.0, float(timeout)))


@dataclass
class ExecutionContext:
    deadline: float
    token: CancellationToken

    @classmethod
    def root(cls, *, max_seconds, token=None, deadline=None):
        return cls(
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

    def wait(self, seconds):
        """Wait within this execution, waking for cancellation or its deadline."""
        self.check_active()
        duration = max(0.0, float(seconds))
        remaining = self.remaining_seconds()
        if self.token.wait(min(duration, remaining)):
            raise ExecutionCancelled(self.token.reason or "execution cancelled")
        self.check_active()

    def request_stop(self, reason="user_cancelled"):
        self.token.request(reason)
