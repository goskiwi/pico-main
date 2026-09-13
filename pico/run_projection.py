"""One small reducer shared by live Run execution and durable replay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .contracts import ToolCall, ToolOutcome
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
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "turn_duration_ms",
            "model_request_count",
            "executed_tool_count",
            "kind_counts",
            "tool_counts",
            "outcome_counts",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid checkpoint RunMetrics")

        def counts(name):
            raw = value[name]
            if not isinstance(raw, dict):
                raise TypeError(f"checkpoint {name} must be an object")
            return {str(key): int(count) for key, count in raw.items()}

        result = cls(
            turn_duration_ms=int(value["turn_duration_ms"]),
            model_request_count=int(value["model_request_count"]),
            executed_tool_count=int(value["executed_tool_count"]),
            kind_counts=counts("kind_counts"),
            tool_counts=counts("tool_counts"),
            outcome_counts=counts("outcome_counts"),
        )
        numeric = (
            result.turn_duration_ms,
            result.model_request_count,
            result.executed_tool_count,
            *result.kind_counts.values(),
            *result.tool_counts.values(),
            *result.outcome_counts.values(),
        )
        if any(item < 0 for item in numeric):
            raise ValueError("checkpoint metric counts cannot be negative")
        return result


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
    metrics: RunMetrics = field(default_factory=RunMetrics)
    status: str = "not_started"
    stop_reason: str = ""
    final_answer: str = ""
    pending_tool: PendingToolCall = field(default_factory=PendingToolCall)
    runtime_feedback: RuntimeFeedback | None = None
    failure_key: tuple[str, str, str, str] = ()
    failure_count: int = 0
    failure_warned: bool = False
    last_sequence: int = 0
    last_timestamp: str = ""

    def check_event(self, event):
        kind = event.kind
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
        self.metrics.apply_event(event)
        self.pending_tool.apply_event(event)
        if event.kind == "failure_observed":
            key = tuple(
                str(event.payload[name])
                for name in ("category", "code", "tool_name", "identity")
            )
            if key == self.failure_key:
                self.failure_count += 1
            else:
                self.failure_key = key
                self.failure_count = 1
            self.failure_warned = self.failure_count >= 3
        elif self._event_makes_progress(event):
            self.failure_key = ()
            self.failure_count = 0
            self.failure_warned = False
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
        self.last_sequence = event.sequence
        self.last_timestamp = event.timestamp
        return self

    def checkpoint_state(self):
        if self.pending_tool.call is not None:
            raise ValueError("cannot checkpoint a pending tool intent")
        if self.status != "running" or self.contract is None:
            raise ValueError("checkpoint requires one active Run")
        return {
            "contract": self.contract.to_dict(),
            "metrics": self.metrics.to_dict(),
            "runtime_feedback": (
                {
                    "instruction": self.runtime_feedback.instruction,
                    "evidence": self.runtime_feedback.evidence,
                    "evidence_artifact_id": self.runtime_feedback.evidence_artifact_id,
                    "event_id": self.runtime_feedback.event_id,
                }
                if self.runtime_feedback
                else None
            ),
            "failure_streak": {
                "key": list(self.failure_key),
                "count": self.failure_count,
            },
        }

    @classmethod
    def from_checkpoint_state(
        cls,
        value,
        *,
        run_id,
        session_id,
        last_sequence,
        last_timestamp,
    ):
        expected = {
            "contract",
            "metrics",
            "runtime_feedback",
            "failure_streak",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid checkpoint state")
        feedback = value["runtime_feedback"]
        if feedback is not None and (
            not isinstance(feedback, dict)
            or set(feedback)
            != {"instruction", "evidence", "evidence_artifact_id", "event_id"}
        ):
            raise ValueError("invalid checkpoint Runtime feedback")
        failure = value["failure_streak"]
        if not isinstance(failure, dict) or set(failure) != {"key", "count"}:
            raise ValueError("invalid checkpoint failure streak")
        if not isinstance(failure["key"], list):
            raise TypeError("checkpoint failure key must be a list")
        failure_key = tuple(str(item) for item in failure["key"])
        if len(failure_key) not in {0, 4}:
            raise ValueError("invalid checkpoint failure key")
        failure_count = int(failure["count"])
        if failure_count < 0:
            raise ValueError("checkpoint failure count cannot be negative")
        projection = cls(
            run_id=str(run_id),
            session_id=str(session_id),
            contract=TaskContract.from_dict(value["contract"]),
            metrics=RunMetrics.from_dict(value["metrics"]),
            status="running",
            runtime_feedback=(RuntimeFeedback(**feedback) if feedback else None),
            failure_key=failure_key,
            failure_count=failure_count,
            failure_warned=failure_count >= 3,
            last_sequence=int(last_sequence),
            last_timestamp=str(last_timestamp),
        )
        if bool(projection.failure_key) != bool(projection.failure_count):
            raise ValueError("checkpoint failure count is inconsistent")
        if projection.last_sequence < 1 or not projection.last_timestamp:
            raise ValueError("checkpoint Projection cursor is invalid")
        return projection

    @staticmethod
    def _event_makes_progress(event):
        if event.kind == "user_guidance":
            return True
        if event.kind == "provider_session_reset":
            return event.payload.get("reason") == "repository_instructions_changed"
        if event.kind not in {"tool_exchange", "tool_settlement"}:
            return False
        outcome = event.payload["outcome"]
        return bool(
            outcome.get("status") == "success"
            or outcome.get("side_effect_state") in {"changed", "partial"}
        )

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
            "failure_streak": {
                "key": list(self.failure_key),
                "count": self.failure_count,
                "warned": self.failure_warned,
            },
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
    metrics: dict[str, Any]

    def __init__(self, projection: RunProjection):
        if not isinstance(projection, RunProjection):
            raise TypeError("RunOutcome requires a RunProjection")
        if not projection.terminal:
            raise ValueError("RunOutcome requires a terminal Run projection")
        object.__setattr__(self, "run_id", projection.run_id)
        object.__setattr__(self, "status", projection.status)
        object.__setattr__(self, "answer", projection.final_answer)
        object.__setattr__(self, "stop_reason", projection.stop_reason)
        object.__setattr__(self, "metrics", projection.metrics.to_dict())

    def to_dict(self):
        """Return a detached view; the terminal Run Log remains authoritative."""

        return {
            "run_id": self.run_id,
            "status": self.status,
            "answer": self.answer,
            "stop_reason": self.stop_reason,
            "metrics": deepcopy(self.metrics),
        }
