"""One small reducer shared by live Run execution and durable replay."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .compaction_summary import CompactedContext
from .contracts import EFFECT_SCOPES, AssistantTurn, ToolCall, ToolOutcome
from .task_state import TaskContract


@dataclass
class RunMetrics:
    attempt_duration_ms: int = 0
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
        elif kind == "tool_result":
            outcome = dict(payload.get("outcome", {}) or {})
            tool = str(outcome.get("tool_name", ""))
            status = str(outcome.get("status", "unknown"))
            if tool and outcome.get("execution_state") != "not_started":
                self.executed_tool_count += 1
                self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1
            self.outcome_counts[status] = self.outcome_counts.get(status, 0) + 1
        elif kind == "assistant_turn":
            turn = AssistantTurn.from_dict(payload["turn"])
            if turn.action.kind == "final":
                self.attempt_duration_ms = int(
                    payload.get("attempt_duration_ms", 0)
                )
        elif kind == "run_stopped":
            self.attempt_duration_ms = int(payload.get("attempt_duration_ms", 0))

    def to_dict(self):
        return {
            "attempt_duration_ms": self.attempt_duration_ms,
            "model_request_count": self.model_request_count,
            "executed_tool_count": self.executed_tool_count,
            "kind_counts": dict(sorted(self.kind_counts.items())),
            "tool_counts": dict(sorted(self.tool_counts.items())),
            "outcome_counts": dict(sorted(self.outcome_counts.items())),
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "attempt_duration_ms",
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
            attempt_duration_ms=int(value["attempt_duration_ms"]),
            model_request_count=int(value["model_request_count"]),
            executed_tool_count=int(value["executed_tool_count"]),
            kind_counts=counts("kind_counts"),
            tool_counts=counts("tool_counts"),
            outcome_counts=counts("outcome_counts"),
        )
        numeric = (
            result.attempt_duration_ms,
            result.model_request_count,
            result.executed_tool_count,
            *result.kind_counts.values(),
            *result.tool_counts.values(),
            *result.outcome_counts.values(),
        )
        if any(item < 0 for item in numeric):
            raise ValueError("checkpoint metric counts cannot be negative")
        return result


@dataclass(frozen=True)
class PendingTool:
    """Current durable tool operation awaiting its settlement."""

    call: ToolCall
    effect_scope: str
    potential_effects: tuple[dict, ...]

    def __post_init__(self):
        if self.effect_scope not in EFFECT_SCOPES - {"none"}:
            raise ValueError("pending tool requires a non-empty effect scope")
        effects = tuple(dict(effect) for effect in self.potential_effects)
        for effect in effects:
            if set(effect) != {"path", "before_state"}:
                raise ValueError("pending tool has an invalid potential effect")
        object.__setattr__(self, "potential_effects", effects)


@dataclass(frozen=True)
class ActiveToolTurn:
    turn: AssistantTurn
    completed_call_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if self.turn.action.kind != "tool":
            raise ValueError("active tool turn requires tool calls")
        call_ids = tuple(call.call_id for call in self.turn.action.tool_calls)
        completed = tuple(str(call_id) for call_id in self.completed_call_ids)
        if completed != call_ids[: len(completed)]:
            raise ValueError("completed tool calls must be an ordered prefix")
        object.__setattr__(self, "completed_call_ids", completed)

    @property
    def next_call(self):
        index = len(self.completed_call_ids)
        calls = self.turn.action.tool_calls
        return calls[index] if index < len(calls) else None


@dataclass(frozen=True)
class ConsecutiveFailure:
    """One repeated model or tool failure currently blocking progress."""

    category: str
    code: str
    tool_name: str
    identity: str
    count: int = 1

    def __post_init__(self):
        if not self.category or not self.code:
            raise ValueError("consecutive failure requires category and code")
        if self.count < 1:
            raise ValueError("consecutive failure count must be positive")

    @property
    def signature(self):
        return self.category, self.code, self.tool_name, self.identity

    def to_dict(self):
        return {
            "category": self.category,
            "code": self.code,
            "tool_name": self.tool_name,
            "identity": self.identity,
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {"category", "code", "tool_name", "identity", "count"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid consecutive failure")
        if not all(isinstance(value[name], str) for name in expected - {"count"}):
            raise TypeError("consecutive failure identity fields must be text")
        return cls(
            category=value["category"],
            code=value["code"],
            tool_name=value["tool_name"],
            identity=value["identity"],
            count=int(value["count"]),
        )


@dataclass
class RunProjection:
    run_id: str = ""
    session_id: str = ""
    contract: TaskContract | None = None
    metrics: RunMetrics = field(default_factory=RunMetrics)
    status: str = "not_started"
    stop_reason: str = ""
    final_answer: str = ""
    pending_tool: PendingTool | None = None
    active_tool_turn: ActiveToolTurn | None = None
    phase: str = "not_started"
    failure: ConsecutiveFailure | None = None
    last_sequence: int = 0

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
        self._check_execution_event(event)

    def _check_execution_event(self, event):
        if event.kind == "compaction":
            context = CompactedContext.from_dict(event.payload["context"])
            if context.covered_through_sequence >= event.sequence:
                raise ValueError("compaction coverage must precede its Event")
        if event.kind == "model_requested":
            if self.phase != "ready_for_model":
                raise ValueError("model request requires a ready Run")
            return
        if event.kind in {"assistant_turn", "model_failure"}:
            if self.phase != "requesting_model":
                raise ValueError(f"{event.kind} requires an active model request")
            return
        if event.kind == "tool_started":
            self._check_tool_started(event)
            return
        if event.kind == "tool_result":
            self._check_tool_result(event)
            return
        if self.pending_tool is not None or self.active_tool_turn is not None:
            raise ValueError("active Assistant turn must receive all Tool Results first")

    def _check_tool_started(self, event):
        if self.phase != "executing_tools" or self.active_tool_turn is None:
            raise ValueError("tool_started requires an active Assistant turn")
        if self.pending_tool is not None:
            raise ValueError("Run Log already has a started tool")
        if event.payload["call_id"] != self.active_tool_turn.next_call.call_id:
            raise ValueError("tool_started must match the next Tool Call")

    def _check_tool_result(self, event):
        outcome = ToolOutcome.from_dict(event.payload["outcome"])
        if self.phase != "executing_tools" or self.active_tool_turn is None:
            raise ValueError("tool_result requires an active Assistant turn")
        call = self.active_tool_turn.next_call
        if (outcome.tool_call_id, outcome.tool_name) != (call.call_id, call.name):
            raise ValueError("tool_result must match the next Tool Call")
        if self.pending_tool is not None:
            if outcome.tool_call_id != self.pending_tool.call.call_id:
                raise ValueError("tool_result must settle the started tool")
            if outcome.execution_state == "not_started":
                raise ValueError("started tool cannot settle as not_started")

    @property
    def terminal(self):
        return self.status in {"completed", "stopped"}

    def apply_event(self, event):
        self.check_event(event)
        return self._advance_event(event)

    def _advance_event(self, event):
        result_call = (
            self.active_tool_turn.next_call
            if event.kind == "tool_result" and self.active_tool_turn is not None
            else None
        )
        self.run_id, self.session_id = (
            event.run_id,
            event.session_id,
        )
        if event.kind == "user_message":
            self.contract = TaskContract.from_dict(event.payload["contract"])
            self.status = "running"
            self.phase = "ready_for_model"
        self.metrics.apply_event(event)
        self._advance_execution(event)
        self._advance_failure(event, result_call)
        self._advance_terminal(event)
        self.last_sequence = event.sequence
        return self

    def _advance_execution(self, event):
        if event.kind in {"run_started", "run_resumed"}:
            self.phase = "ready_for_model"
        elif event.kind == "model_requested":
            self.phase = "requesting_model"
        elif event.kind == "assistant_turn":
            turn = AssistantTurn.from_dict(event.payload["turn"])
            if turn.action.kind == "tool":
                self.active_tool_turn = ActiveToolTurn(turn)
                self.phase = "executing_tools"
            else:
                self.status = "completed"
                self.stop_reason = "final_answer_returned"
                self.final_answer = turn.action.content
                self.phase = "completed"
        elif event.kind == "model_failure":
            self.phase = "ready_for_model"
        elif event.kind == "tool_started":
            call = self.active_tool_turn.next_call
            self.pending_tool = PendingTool(
                call=call,
                effect_scope=str(event.payload["effect_scope"]),
                potential_effects=tuple(event.payload["potential_effects"]),
            )
        elif event.kind == "tool_result":
            call_id = str(event.payload["outcome"]["tool_call_id"])
            self.pending_tool = None
            completed = (*self.active_tool_turn.completed_call_ids, call_id)
            if len(completed) == len(self.active_tool_turn.turn.action.tool_calls):
                self.active_tool_turn = None
                self.phase = "ready_for_model"
            else:
                self.active_tool_turn = ActiveToolTurn(
                    self.active_tool_turn.turn,
                    completed,
                )

    def _advance_failure(self, event, result_call):
        if event.kind == "model_failure":
            observed = ConsecutiveFailure(
                category="model",
                code=str(event.payload["kind"]),
                tool_name="",
                identity=str(event.payload["identity"]),
            )
            self._apply_failure(observed)
        elif event.kind == "tool_result":
            outcome = ToolOutcome.from_dict(event.payload["outcome"])
            if outcome.status != "success":
                call = result_call
                if call is None:
                    raise ValueError("tool result has no active Tool Call")
                observed = ConsecutiveFailure(
                    category="tool",
                    code=(outcome.failure.code if outcome.failure else outcome.status),
                    tool_name=outcome.tool_name,
                    identity=json.dumps(
                        {
                            "input": call.args,
                            "detail": (
                                outcome.failure.detail
                                if outcome.failure
                                else outcome.content
                            ),
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                self._apply_failure(observed)
            elif self._event_makes_progress(event):
                self.failure = None
        elif event.kind == "assistant_turn":
            if self.failure is not None and self.failure.category == "model":
                self.failure = None
        elif self._event_clears_failure(event):
            self.failure = None

    def _advance_terminal(self, event):
        if event.kind == "run_stopped":
            self.status = "stopped"
            self.stop_reason = event.payload["stop_reason"]
            self.final_answer = str(event.payload.get("content", ""))
            self.phase = "stopped"

    def _apply_failure(self, observed):
        if self.failure is not None and observed.signature == self.failure.signature:
            observed = ConsecutiveFailure(
                *observed.signature,
                count=self.failure.count + 1,
            )
        self.failure = observed

    def checkpoint_state(self):
        if (
            self.pending_tool is not None
            or self.active_tool_turn is not None
            or self.phase != "ready_for_model"
        ):
            raise ValueError("checkpoint requires a ready-for-model boundary")
        if self.status != "running" or self.contract is None:
            raise ValueError("checkpoint requires one active Run")
        return {
            "contract": self.contract.to_dict(),
            "status": self.status,
            "metrics": self.metrics.to_dict(),
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }

    @classmethod
    def from_checkpoint_state(
        cls,
        value,
        *,
        run_id,
        session_id,
        last_sequence,
    ):
        expected = {
            "contract",
            "status",
            "metrics",
            "failure",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid checkpoint state")
        if value["status"] != "running":
            raise ValueError("checkpoint task status must be running")
        failure = value["failure"]
        if failure is not None:
            failure = ConsecutiveFailure.from_dict(failure)
        projection = cls(
            run_id=str(run_id),
            session_id=str(session_id),
            contract=TaskContract.from_dict(value["contract"]),
            metrics=RunMetrics.from_dict(value["metrics"]),
            status="running",
            failure=failure,
            last_sequence=int(last_sequence),
            phase="ready_for_model",
        )
        if projection.last_sequence < 1:
            raise ValueError("checkpoint Projection cursor is invalid")
        return projection

    @staticmethod
    def _event_makes_progress(event):
        if event.kind == "user_guidance":
            return True
        if event.kind == "provider_session_reset":
            return event.payload.get("reason") == "project_instructions_changed"
        if event.kind != "tool_result":
            return False
        outcome = event.payload["outcome"]
        return bool(
            outcome.get("status") == "success"
            or outcome.get("side_effect_state") in {"changed", "partial"}
        )

    def _event_clears_failure(self, event):
        if self.failure is not None and self.failure.category == "model":
            return event.kind == "assistant_turn"
        return self._event_makes_progress(event)

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
            "pending_call_id": (
                self.pending_tool.call.call_id if self.pending_tool else ""
            ),
            "phase": self.phase,
            "active_tool_calls": (
                [
                    {
                        "name": call.name,
                        "args": dict(call.args),
                        "call_id": call.call_id,
                        "state": (
                            "completed"
                            if call.call_id
                            in self.active_tool_turn.completed_call_ids
                            else (
                                "started"
                                if self.pending_tool is not None
                                and call.call_id == self.pending_tool.call.call_id
                                else "not_started"
                            )
                        ),
                    }
                    for call in self.active_tool_turn.turn.action.tool_calls
                ]
                if self.active_tool_turn is not None
                else []
            ),
            "failure": self.failure.to_dict() if self.failure is not None else None,
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
