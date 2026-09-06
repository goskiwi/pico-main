"""One small reducer shared by live Run execution and durable replay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .contracts import ToolCall, ToolOutcome
from .delivery import FinalDiff
from .evidence import RunEvidence
from .subagents.contracts import ChildState
from .task_state import TaskContract
from .working_state import WorkingState


@dataclass(frozen=True)
class RunCursor:
    sequence: int = 0
    event_id: str = ""

    def to_dict(self):
        return {"sequence": self.sequence, "event_id": self.event_id}


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
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    contract: TaskContract | None = None
    working: WorkingState = field(default_factory=WorkingState)
    evidence: RunEvidence = field(default_factory=RunEvidence)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    children: ChildState = field(default_factory=ChildState)
    status: str = "running"
    stop_reason: str = ""
    final_answer: str = ""
    pending_calls: tuple[ToolCall, ...] = ()
    pending_group_id: str = ""
    pending_runtime_instruction: str = ""
    pending_runtime_instruction_event_id: str = ""
    pending_runtime_instruction_code: str = ""
    pending_runtime_evidence: str = ""
    pending_runtime_evidence_artifact_id: str = ""
    started_call_ids: set[str] = field(default_factory=set)
    last_started_ordinal: int = -1
    start_phase_result_count: int = 0
    result_count: int = 0
    final_diff: FinalDiff | None = None
    last_cursor: RunCursor = field(default_factory=RunCursor)

    def check_event(self, event):  # noqa: C901 - linear protocol state machine
        kind, payload = event.kind, event.payload
        expected = self.last_cursor.sequence + 1
        if event.sequence != expected:
            raise ValueError("Run Log sequence is not contiguous")
        if event.event_id != f"{event.run_id}:event:{expected:06d}":
            raise ValueError("Run event id does not match its sequence")
        if self.run_id and (event.run_id, event.task_id, event.session_id) != (
            self.run_id,
            self.task_id,
            self.session_id,
        ):
            raise ValueError("Run event identity changed within one run")
        if self.terminal:
            raise ValueError("Run Log cannot append after a terminal event")
        if self.contract is None and kind != "user_message":
            raise ValueError("Run Log must begin with user_message")
        if kind == "user_message" and self.contract is not None:
            raise ValueError("Run Log may contain only one user_message")
        if kind == "assistant_tool_calls":
            if self.pending_calls:
                raise ValueError("Run Log already has pending tool calls")
        elif kind == "tool_started":
            call_id = str(payload["tool_call_id"])
            call = self._pending_call(call_id)
            if call is None:
                raise ValueError("tool_started must match a pending tool call")
            if str(payload["tool_name"]) != call.name:
                raise ValueError(
                    "tool_started tool name does not match the pending call"
                )
            if call_id in self.started_call_ids:
                raise ValueError("pending tool call already started")
            ordinal = self.pending_calls.index(call)
            expected_ordinal = max(self.result_count, self.last_started_ordinal + 1)
            if ordinal != expected_ordinal:
                raise ValueError("tool_started calls must preserve group order")
            unresolved_started = self.result_count <= self.last_started_ordinal
            if (
                unresolved_started
                and self.result_count != self.start_phase_result_count
            ):
                raise ValueError(
                    "tool_started cannot cross an unfinished execution barrier"
                )
        elif kind == "tool_result":
            outcome = ToolOutcome.from_dict(payload["outcome"])
            call_id = outcome.tool_call_id
            if not self.pending_calls or self.result_count >= len(self.pending_calls):
                raise ValueError("tool_result requires pending tool calls")
            call = self.pending_calls[self.result_count]
            if call_id != call.call_id:
                raise ValueError("tool_result calls must preserve group order")
            if outcome.tool_name != call.name:
                raise ValueError(
                    "tool_result tool name does not match the pending call"
                )
            started = call_id in self.started_call_ids
            if outcome.execution_state == "not_started" and started:
                raise ValueError("started tool cannot finish as not_started")
            if outcome.execution_state != "not_started" and not started:
                raise ValueError("executed tool_result requires tool_started")
            self.children.check_result(call, payload["outcome"])
        elif self.pending_calls and kind not in {"tool_started", "tool_result"}:
            raise ValueError("pending tool calls must receive results first")

        if kind in {"assistant_final", "run_stopped"}:
            raw = payload.get("final_diff")
            final_diff = FinalDiff.from_dict(raw) if raw is not None else None
            if kind == "assistant_final" and final_diff is None:
                raise ValueError("completed Run requires a final Diff")
            if final_diff is not None and bool(self.evidence.changed_paths) != bool(
                final_diff.artifact_id
            ):
                raise ValueError("terminal final Diff does not match net changes")

    def _pending_call(self, call_id):
        return next(
            (call for call in self.pending_calls if call.call_id == call_id), None
        )

    def _begin_calls(self, calls, group_id=""):
        self.pending_calls = tuple(calls)
        self.pending_group_id = group_id
        self.started_call_ids.clear()
        self.last_started_ordinal = -1
        self.start_phase_result_count = 0
        self.result_count = 0

    @property
    def terminal(self):
        return self.status in {"completed", "stopped"}

    @property
    def pending_call_ids(self):
        return tuple(call.call_id for call in self.pending_calls[self.result_count :])

    @property
    def pending_call_id(self):
        ids = self.pending_call_ids
        return ids[0] if len(ids) == 1 else None

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
        self.run_id, self.task_id, self.session_id = (
            event.run_id,
            event.task_id,
            event.session_id,
        )
        if event.kind == "user_message":
            self.contract = TaskContract.from_dict(event.payload["contract"])
        self.working.apply_event(event)
        self.evidence.apply_event(event)
        self.metrics.apply_event(event)
        if event.kind == "tool_result":
            self.children.apply_result(
                self.pending_calls[self.result_count], event.payload["outcome"]
            )
        if event.kind == "assistant_tool_calls":
            self.pending_runtime_instruction = ""
            self.pending_runtime_instruction_event_id = ""
            self.pending_runtime_instruction_code = ""
            self.pending_runtime_evidence = ""
            self.pending_runtime_evidence_artifact_id = ""
            self._begin_calls(event.tool_calls, event.event_id)
        elif event.kind == "tool_started":
            if self.result_count > self.last_started_ordinal:
                self.start_phase_result_count = self.result_count
            self.started_call_ids.add(event.call_id)
            self.last_started_ordinal = self.pending_calls.index(
                self._pending_call(event.call_id)
            )
        elif event.kind == "tool_result":
            self.result_count += 1
            if self.result_count == len(self.pending_calls):
                self._begin_calls(())
        elif event.kind == "model_instruction":
            self.pending_runtime_instruction = str(event.payload["instruction"])
            self.pending_runtime_instruction_event_id = event.event_id
            self.pending_runtime_instruction_code = str(event.payload["code"])
            self.pending_runtime_evidence = str(event.payload["evidence"])
            self.pending_runtime_evidence_artifact_id = str(
                event.payload["evidence_artifact_id"]
            )
        elif event.kind in {"assistant_final", "run_stopped"}:
            self.pending_runtime_instruction = ""
            self.pending_runtime_instruction_event_id = ""
            self.pending_runtime_instruction_code = ""
            self.pending_runtime_evidence = ""
            self.pending_runtime_evidence_artifact_id = ""
            self.status = "completed" if event.kind == "assistant_final" else "stopped"
            self.stop_reason = event.payload["stop_reason"]
            self.final_answer = str(event.payload.get("content", ""))
            raw = event.payload.get("final_diff")
            self.final_diff = FinalDiff.from_dict(raw) if raw is not None else None
        self.last_cursor = RunCursor(event.sequence, event.event_id)
        return self

    def summary(self):
        if self.contract is None:
            raise ValueError("Run projection has no task")
        return {
            "identity": {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "session_id": self.session_id,
            },
            "task": {
                "contract": self.contract.to_dict(),
                "working": self.working.to_dict(),
                "lifecycle": {
                    "status": self.status,
                    "stop_reason": self.stop_reason,
                    "final_answer": self.final_answer,
                },
            },
            "evidence": self.evidence.to_dict(),
            "metrics": self.metrics.to_dict(),
            "runtime_instruction": {
                "code": self.pending_runtime_instruction_code,
                "instruction": self.pending_runtime_instruction,
                "evidence": self.pending_runtime_evidence,
                "evidence_artifact_id": (
                    self.pending_runtime_evidence_artifact_id
                ),
            },
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
