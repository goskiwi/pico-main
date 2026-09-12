"""One small reducer shared by live Run execution and durable replay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .contracts import ToolCall, ToolOutcome
from .delivery import FinalDiff
from .evidence import RunEvidence
from .task_state import TaskContract


@dataclass(frozen=True)
class RuntimeFeedback:
    instruction: str
    evidence: str = ""
    evidence_artifact_id: str = ""
    event_id: str = ""


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
        elif kind in {"tool_exchange", "tool_settlement"}:
            outcome = dict(payload.get("outcome", {}) or {})
            tool = str(outcome.get("tool_name", ""))
            status = str(outcome.get("status", "unknown"))
            if tool and outcome.get("execution_state") != "not_started":
                self.executed_tool_count += 1
                self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1
            self.outcome_counts[status] = self.outcome_counts.get(status, 0) + 1
            if tool == "verify":
                verification = dict(outcome.get("structured", {})).get("verification", {})
                verification_status = str(verification.get("status", "unknown"))
                self.verification_counts[verification_status] = (
                    self.verification_counts.get(verification_status, 0) + 1
                )
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
class PendingToolCall:
    """Track the one durable effect intent that may need recovery."""

    call: ToolCall | None = None

    def check_event(self, event):
        kind, payload = event.kind, event.payload
        if kind == "tool_intent":
            if self.call is not None:
                raise ValueError("Run Log already has a pending tool intent")
        elif kind == "tool_settlement":
            outcome = ToolOutcome.from_dict(payload["outcome"])
            if self.call is None:
                raise ValueError("tool_settlement requires a pending tool intent")
            if outcome.tool_call_id != self.call.call_id:
                raise ValueError("tool_settlement must match the pending call id")
            if outcome.tool_name != self.call.name:
                raise ValueError(
                    "tool_settlement tool name does not match the pending call"
                )
            if outcome.execution_state == "not_started":
                raise ValueError("persisted tool intent cannot settle as not_started")
        elif self.call is not None:
            raise ValueError("pending tool intent must receive a settlement first")

    def apply_event(self, event):
        if event.kind == "tool_intent":
            self.call = event.tool_call
        elif event.kind == "tool_settlement":
            self.call = None


@dataclass
class RunProjection:
    run_id: str = ""
    session_id: str = ""
    contract: TaskContract | None = None
    evidence: RunEvidence = field(default_factory=RunEvidence)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    status: str = "not_started"
    stop_reason: str = ""
    final_answer: str = ""
    pending_tool: PendingToolCall = field(default_factory=PendingToolCall)
    runtime_feedback: RuntimeFeedback | None = None
    final_diff: FinalDiff | None = None
    last_sequence: int = 0

    def check_event(self, event):
        kind, payload = event.kind, event.payload
        expected = self.last_sequence + 1
        if event.sequence != expected:
            raise ValueError("Run Log sequence is not contiguous")
        if event.event_id != f"{event.run_id}:event:{expected:06d}":
            raise ValueError("Run event id does not match its sequence")
        if self.run_id and (event.run_id, event.session_id) != (
            self.run_id,
            self.session_id,
        ):
            raise ValueError("Run event identity changed within one run")
        if self.terminal:
            raise ValueError("Run Log cannot append after a terminal event")
        if self.contract is None and kind != "user_message":
            raise ValueError("Run Log must begin with user_message")
        if kind == "user_message" and self.contract is not None:
            raise ValueError("Run Log may contain only one user_message")
        self.pending_tool.check_event(event)
        if kind in {"assistant_final", "run_stopped"}:
            raw = payload.get("final_diff")
            final_diff = FinalDiff.from_dict(raw) if raw is not None else None
            if kind == "assistant_final" and final_diff is None:
                raise ValueError("completed Run requires a final Diff")
            if final_diff is not None and bool(self.evidence.changed_paths) != bool(
                final_diff.artifact_id
            ):
                raise ValueError("terminal final Diff does not match net changes")
            if final_diff is not None:
                external = set(self.evidence.external_paths)
                if set(final_diff.external_paths) != external:
                    raise ValueError("terminal final Diff omits or misstates observed external changes")

    @property
    def terminal(self):
        return self.status in {"completed", "stopped"}

    @property
    def pending_call_id(self):
        return self.pending_tool.call.call_id if self.pending_tool.call else ""

    @property
    def model_request_count(self):
        return self.metrics.model_request_count

    @property
    def executed_tool_count(self):
        return self.metrics.executed_tool_count

    @property
    def turn_duration_ms(self):
        return self.metrics.turn_duration_ms

    def apply_event(self, event):
        self.check_event(event)
        return self._advance_event(event)

    def _advance_event(self, event):
        self.run_id, self.session_id = (
            event.run_id,
            event.session_id,
        )
        if event.kind == "user_message":
            self.contract = TaskContract.from_dict(event.payload["contract"])
            self.status = "running"
        self.evidence.apply_event(event)
        self.metrics.apply_event(event)
        self.pending_tool.apply_event(event)
        if event.kind in {"tool_exchange", "tool_intent"}:
            self.runtime_feedback = None
        elif event.kind == "model_instruction":
            self.runtime_feedback = RuntimeFeedback(
                instruction=str(event.payload["instruction"]),
                evidence=str(event.payload["evidence"]),
                evidence_artifact_id=str(event.payload["evidence_artifact_id"]),
                event_id=event.event_id,
            )
        elif event.kind in {"assistant_final", "run_stopped"}:
            self.runtime_feedback = None
            self.status = "completed" if event.kind == "assistant_final" else "stopped"
            self.stop_reason = event.payload["stop_reason"]
            self.final_answer = str(event.payload.get("content", ""))
            raw = event.payload.get("final_diff")
            self.final_diff = FinalDiff.from_dict(raw) if raw is not None else None
        self.last_sequence = event.sequence
        return self

    def summary(self):
        if self.contract is None:
            raise ValueError("Run projection has no task")
        return {
            "identity": {
                "run_id": self.run_id,
                "session_id": self.session_id,
            },
            "task": {
                "contract": self.contract.to_dict(),
                "lifecycle": {
                    "status": self.status,
                    "stop_reason": self.stop_reason,
                    "final_answer": self.final_answer,
                },
            },
            "evidence": self.evidence.to_dict(),
            "metrics": self.metrics.to_dict(),
            "runtime_feedback": (
                {
                    "instruction": self.runtime_feedback.instruction,
                    "evidence": self.runtime_feedback.evidence,
                    "evidence_artifact_id": self.runtime_feedback.evidence_artifact_id,
                    "event_id": self.runtime_feedback.event_id,
                }
                if self.runtime_feedback is not None
                else None
            ),
            "pending_call_id": self.pending_call_id,
            "final_diff": self.final_diff.to_dict() if self.final_diff else None,
            "last_sequence": self.last_sequence,
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
    final_diff: FinalDiff | None
    changed_paths: tuple[str, ...]
    metrics: dict[str, Any]

    def __init__(self, projection: RunProjection):
        if not isinstance(projection, RunProjection):
            raise TypeError("RunOutcome requires a RunProjection")
        if not projection.terminal:
            raise ValueError("RunOutcome requires a terminal Run projection")
        if projection.status == "completed" and projection.final_diff is None:
            raise ValueError("completed Run projection requires a final Diff")
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
            "final_diff": self.final_diff.to_dict() if self.final_diff else None,
            "changed_paths": list(self.changed_paths),
            "metrics": deepcopy(self.metrics),
        }
