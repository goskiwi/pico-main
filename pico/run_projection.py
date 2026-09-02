"""One small reducer shared by live Run execution and durable replay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .delivery import FinalDiffDescriptor
from .evidence import RunEvidence
from .task_state import TaskContract, TaskState


@dataclass(frozen=True)
class RunCursor:
    sequence: int = 0
    event_id: str = ""

    def to_dict(self):
        return {"sequence": self.sequence, "event_id": self.event_id}


@dataclass
class RunIdentity:
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""

    def observe(self, event):
        candidate = (str(event.run_id), str(event.task_id), str(event.session_id))
        current = (self.run_id, self.task_id, self.session_id)
        if self.run_id and candidate != current:
            raise ValueError("Run event identity changed inside one projection")
        self.run_id, self.task_id, self.session_id = candidate

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
        }


@dataclass
class RunMetrics:
    turn_duration_ms: int = 0
    model_request_count: int = 0
    executed_tool_count: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    verification_counts: dict[str, int] = field(default_factory=dict)

    def apply_event(self, event):
        kind = event.kind
        payload = dict(event.payload)
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        if kind == "model_requested":
            self.model_request_count += 1
        elif kind == "tool_result":
            outcome = dict(payload.get("outcome", {}) or {})
            tool = str(outcome.get("tool_name", ""))
            status = str(outcome.get("status", "unknown"))
            if tool and outcome.get("execution_state") != "not_started":
                self.executed_tool_count += 1
                self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1
            self.outcome_counts[status] = self.outcome_counts.get(status, 0) + 1
        elif kind == "verification_result":
            status = str(payload.get("status", "unknown"))
            self.verification_counts[status] = (
                self.verification_counts.get(status, 0) + 1
            )
        elif kind in {"assistant_final", "run_stopped"}:
            self.turn_duration_ms = int(payload.get("turn_duration_ms", 0))

    def to_dict(self):
        return {
            "turn_duration_ms": self.turn_duration_ms,
            "model_request_count": self.model_request_count,
            "executed_tool_count": self.executed_tool_count,
            "kind_counts": dict(sorted(self.kind_counts.items())),
            "tool_counts": dict(sorted(self.tool_counts.items())),
            "outcome_counts": dict(sorted(self.outcome_counts.items())),
            "verification_counts": dict(sorted(self.verification_counts.items())),
        }


@dataclass
class RunProjection:
    identity: RunIdentity = field(default_factory=RunIdentity)
    task: TaskState | None = None
    evidence: RunEvidence = field(default_factory=RunEvidence)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    pending_call_ids: tuple[str, ...] = ()
    final_diff: FinalDiffDescriptor | None = None
    last_cursor: RunCursor = field(default_factory=RunCursor)

    def apply_event(self, event):
        self.identity.observe(event)
        if event.kind == "user_message":
            if self.task is not None:
                raise ValueError("Run projection may contain only one task contract")
            self.task = TaskState.create(
                TaskContract.from_dict(event.payload["contract"])
            )
        elif self.task is None:
            raise ValueError("Run projection requires task contract before other events")
        else:
            self.task.apply_event(event)

        self.evidence.apply_event(event)
        self.metrics.apply_event(event)
        if event.kind == "assistant_tool_call":
            self.pending_call_ids = (event.call_id,)
        elif event.kind == "assistant_tool_batch":
            self.pending_call_ids = tuple(
                call.call_id for call in event.batch_calls
            )
        elif event.kind == "tool_result":
            if not self.pending_call_ids or event.call_id != self.pending_call_ids[0]:
                raise ValueError("tool result does not match projected pending order")
            self.pending_call_ids = self.pending_call_ids[1:]
        if event.kind in {"assistant_final", "run_stopped"}:
            self.final_diff = FinalDiffDescriptor.from_dict(
                event.payload["final_diff"]
            )
            if (
                event.kind == "assistant_final"
                and self.final_diff.unavailable_reason
            ):
                raise ValueError("completed Run requires an available final Diff")
            if (
                not self.final_diff.unavailable_reason
                and bool(self.evidence.changed_paths)
                != bool(self.final_diff.diff_artifact_id)
            ):
                raise ValueError("terminal final Diff does not match net changes")
        self.last_cursor = RunCursor(event.sequence, event.event_id)
        return self

    @property
    def terminal(self):
        return bool(
            self.task
            and self.task.lifecycle.status in {"completed", "stopped"}
        )

    @property
    def pending_call_id(self):
        return self.pending_call_ids[0] if len(self.pending_call_ids) == 1 else None

    @property
    def run_id(self):
        return self.identity.run_id

    @property
    def task_id(self):
        return self.identity.task_id

    @property
    def session_id(self):
        return self.identity.session_id

    @property
    def status(self):
        return self.task.lifecycle.status if self.task else "running"

    @property
    def stop_reason(self):
        return self.task.lifecycle.stop_reason if self.task else ""

    @property
    def final_answer(self):
        return self.task.lifecycle.final_answer if self.task else ""

    @property
    def model_request_count(self):
        return self.metrics.model_request_count

    @property
    def executed_tool_count(self):
        return self.metrics.executed_tool_count

    @property
    def turn_duration_ms(self):
        return self.metrics.turn_duration_ms

    def summary(self):
        if self.task is None:
            raise ValueError("Run projection has no task")
        return {
            "identity": self.identity.to_dict(),
            "task": self.task.to_dict(),
            "evidence": self.evidence.to_dict(),
            "metrics": self.metrics.to_dict(),
            "pending_call_ids": list(self.pending_call_ids),
            "final_diff": self.final_diff.to_dict() if self.final_diff else None,
            "run_cursor": self.last_cursor.to_dict(),
        }


@dataclass(frozen=True, slots=True, init=False)
class RunOutcome:
    """Frozen public result envelope captured from one terminal Run projection.

    Metrics are a detached dictionary snapshot rather than deeply immutable
    state.  The terminal Run Log and its replayed :class:`RunProjection` remain
    the source of truth.
    """

    run_id: str
    status: str
    answer: str
    stop_reason: str
    final_diff: FinalDiffDescriptor
    changed_paths: tuple[str, ...]
    metrics: dict[str, Any]

    def __init__(self, projection: RunProjection):
        if not isinstance(projection, RunProjection):
            raise TypeError("RunOutcome requires a RunProjection")
        if not projection.terminal:
            raise ValueError("RunOutcome requires a terminal Run projection")
        if projection.final_diff is None:
            raise ValueError("terminal Run projection requires a final Diff")
        object.__setattr__(self, "run_id", projection.run_id)
        object.__setattr__(self, "status", projection.status)
        object.__setattr__(self, "answer", projection.final_answer)
        object.__setattr__(self, "stop_reason", projection.stop_reason)
        object.__setattr__(self, "final_diff", projection.final_diff)
        object.__setattr__(
            self,
            "changed_paths",
            tuple(projection.evidence.changed_paths),
        )
        object.__setattr__(self, "metrics", projection.metrics.to_dict())

    def to_dict(self):
        """Return a detached view; the terminal Run Log remains authoritative."""

        return {
            "run_id": self.run_id,
            "status": self.status,
            "answer": self.answer,
            "stop_reason": self.stop_reason,
            "final_diff": self.final_diff.to_dict(),
            "changed_paths": list(self.changed_paths),
            "metrics": deepcopy(self.metrics),
        }
