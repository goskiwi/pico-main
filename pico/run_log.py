"""Single durable event log for one Pico run."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .compaction_summary import CompactedContext
from .contracts import EFFECT_SCOPES, AssistantTurn, ToolOutcome
from .history import CONTEXT_KINDS, ContextState, RunHistory
from .run_projection import RunProjection
from .task_state import TaskContract

RUN_EVENT_KINDS = frozenset(
    {
        *CONTEXT_KINDS,
        "user_message",
        "run_started",
        "run_resumed",
        "model_requested",
        "assistant_turn",
        "model_failure",
        "tool_started",
        "tool_result",
        "provider_session_reset",
        "run_stopped",
    }
)


def _exact_payload(kind, payload, required, optional=()):
    required = set(required)
    allowed = required | set(optional)
    if set(payload) != required and not (
        required <= set(payload) and set(payload) <= allowed
    ):
        raise ValueError(f"invalid {kind} payload fields")


def _validate_text_payload(kind, payload):
    _exact_payload(kind, payload, {"content"})
    if not isinstance(payload["content"], str):
        raise TypeError(f"{kind} content must be text")


def _validate_compaction_payload(kind, payload):
    _exact_payload(kind, payload, {"context"})
    CompactedContext.from_dict(payload["context"])


def _validate_assistant_turn_payload(kind, payload):
    _exact_payload(kind, payload, {"turn", "attempt_duration_ms"})
    AssistantTurn.from_dict(payload["turn"])
    if int(payload["attempt_duration_ms"]) < 0:
        raise ValueError("assistant turn duration cannot be negative")


def _validate_model_failure_payload(kind, payload):
    _exact_payload(kind, payload, {"kind", "identity", "detail", "usage"})
    if payload["kind"] not in {
        "invalid",
        "provider_failure",
        "interrupted",
        "context_overflow",
    }:
        raise ValueError("model_failure has an invalid kind")
    if not isinstance(payload["identity"], str) or not isinstance(
        payload["detail"], str
    ):
        raise TypeError("model_failure identity and detail must be text")
    if not isinstance(payload["usage"], dict):
        raise TypeError("model_failure usage must be an object")


def _validate_user_payload(kind, payload):
    _exact_payload(kind, payload, {"contract"})
    TaskContract.from_dict(payload["contract"])


def _validate_tool_started_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {
            "call_id",
            "effect_scope",
            "potential_effects",
            "operation",
        },
    )
    if not isinstance(payload["call_id"], str) or not payload["call_id"]:
        raise ValueError("tool_started requires a call id")
    if payload["effect_scope"] not in EFFECT_SCOPES - {"none"}:
        raise ValueError("tool_started requires a non-empty effect scope")
    if not isinstance(payload["potential_effects"], list) or not isinstance(
        payload["operation"], dict
    ):
        raise TypeError("tool_started has invalid field types")
    for effect in payload["potential_effects"]:
        if not isinstance(effect, dict) or set(effect) != {
            "path",
            "before_state",
        }:
            raise ValueError("tool_started has invalid potential effect")


def _validate_tool_result_payload(kind, payload):
    _exact_payload(kind, payload, {"outcome"}, {"recovered_from_interruption"})
    ToolOutcome.from_dict(payload["outcome"])
    if "recovered_from_interruption" in payload and not isinstance(
        payload["recovered_from_interruption"], bool
    ):
        raise TypeError("tool_result recovery marker must be boolean")


def _validate_stopped_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"content", "stop_reason", "attempt_duration_ms"},
    )
    if not str(payload["stop_reason"]):
        raise ValueError("run_stopped requires stop_reason")
    if int(payload["attempt_duration_ms"]) < 0:
        raise ValueError("run_stopped duration cannot be negative")


_PAYLOAD_VALIDATORS = {
    "user_message": _validate_user_payload,
    "user_guidance": _validate_text_payload,
    "assistant_turn": _validate_assistant_turn_payload,
    "compaction": _validate_compaction_payload,
    "model_failure": _validate_model_failure_payload,
    "tool_started": _validate_tool_started_payload,
    "tool_result": _validate_tool_result_payload,
    "run_stopped": _validate_stopped_payload,
}


def _validate_event_payload(kind, payload):
    validator = _PAYLOAD_VALIDATORS.get(kind)
    if validator is not None:
        validator(kind, payload)


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    sequence: int
    run_id: str
    session_id: str
    kind: str
    timestamp: str
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in RUN_EVENT_KINDS:
            raise ValueError(f"unsupported Run Log kind: {self.kind}")
        if self.sequence < 1:
            raise ValueError("Run Log sequence must be positive")
        if not isinstance(self.payload, dict):
            raise TypeError("Run Log payload must be an object")
        _validate_event_payload(self.kind, self.payload)

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "event_id",
            "sequence",
            "run_id",
            "session_id",
            "kind",
            "timestamp",
            "payload",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid Run event")
        return cls(
            event_id=str(value["event_id"]),
            sequence=int(value["sequence"]),
            run_id=str(value["run_id"]),
            session_id=str(value["session_id"]),
            kind=str(value["kind"]),
            timestamp=str(value["timestamp"]),
            payload=dict(value["payload"]),
        )

    @property
    def content(self):
        if self.kind == "user_message":
            return str(dict(self.payload.get("contract", {})).get("goal", ""))
        if self.kind == "user_guidance":
            return str(self.payload.get("content", ""))
        if self.kind == "assistant_turn":
            turn = AssistantTurn.from_dict(self.payload["turn"])
            return turn.text or turn.action.content
        if self.kind == "tool_result":
            outcome = dict(self.payload.get("outcome", {}) or {})
            return str(outcome.get("content", ""))
        if self.kind == "compaction":
            return CompactedContext.from_dict(self.payload["context"]).render()
        return ""

    @property
    def name(self):
        if self.kind == "tool_result":
            return str(dict(self.payload.get("outcome", {}) or {}).get("tool_name", ""))
        return ""

    @property
    def call_id(self):
        if self.kind in {"tool_started", "tool_result"}:
            if self.kind == "tool_started":
                return str(self.payload["call_id"])
            return str(
                dict(self.payload.get("outcome", {}) or {}).get("tool_call_id", "")
            )
        return ""

    @property
    def artifact_id(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(outcome.get("artifact_id", ""))

    @property
    def covered_through_sequence(self):
        if self.kind != "compaction":
            return 0
        return CompactedContext.from_dict(
            self.payload["context"]
        ).covered_through_sequence


def replay_events(events, *, expected_run_id=None):
    projection = RunProjection()
    for event in events:
        if expected_run_id is not None and event.run_id != str(expected_run_id):
            raise ValueError("Run event belongs to another run")
        projection.apply_event(event)
    return projection


class RunLog:
    """Own event construction, protocol validation and one Run's accepted facts."""

    def __init__(self, run_id, session_id, store):
        self.run_id = str(run_id)
        self.session_id = str(session_id)
        self.store = store
        self.context_state = ContextState()
        self.projection = RunProjection(
            run_id=self.run_id,
            session_id=self.session_id,
        )

    @classmethod
    def _from_events(cls, events, store, *, expected_run_id):
        """Restore the writer and its Projection from one storage snapshot."""
        events = tuple(events)
        if not events:
            raise ValueError("active Run Log is missing or empty")
        projection = replay_events(events, expected_run_id=expected_run_id)
        first = events[0]
        log = cls(first.run_id, first.session_id, store)
        log.projection = projection
        log.context_state = ContextState.from_events(events)
        return log

    @classmethod
    def _from_checkpoint(
        cls,
        *,
        projection,
        context_state,
        tail_events,
        store,
    ):
        log = cls(projection.run_id, projection.session_id, store)
        log.projection = projection
        log.context_state = context_state
        for event in tail_events:
            log.projection.apply_event(event)
            log._apply_context_event(event)
        return log

    def history(self):
        return RunHistory(context_state=self.context_state)

    def _apply_context_event(self, entry):
        self.context_state.apply_event(entry)

    def append(self, kind, payload=None):
        sequence = self.projection.last_sequence + 1
        entry = RunEvent(
            event_id=f"{self.run_id}:event:{sequence:06d}",
            sequence=sequence,
            run_id=self.run_id,
            session_id=self.session_id,
            kind=str(kind),
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=dict(payload or {}),
        )
        candidate = deepcopy(self.projection)
        candidate.apply_event(entry)
        self.store._append_event(entry)
        self._apply_context_event(entry)
        self.projection.__dict__.update(candidate.__dict__)
        if self.store.trace is not None:
            self.store.trace(entry)
        return entry

    def append_user(self, contract):
        if not isinstance(contract, TaskContract):
            raise TypeError("user_message requires a TaskContract")
        return self.append("user_message", {"contract": contract.to_dict()})

    def append_user_guidance(self, content):
        content = str(content).strip()
        if not content:
            raise ValueError("user guidance must not be blank")
        self._require_ready()
        return self.append("user_guidance", {"content": content})

    def append_assistant_turn(self, turn, *, attempt_duration_ms=0):
        if not isinstance(turn, AssistantTurn):
            raise TypeError("assistant_turn requires an AssistantTurn")
        return self.append(
            "assistant_turn",
            {
                "turn": turn.to_dict(),
                "attempt_duration_ms": int(attempt_duration_ms),
            },
        )

    def append_model_failure(self, kind, identity, detail, usage):
        return self.append(
            "model_failure",
            {
                "kind": str(kind),
                "identity": str(identity),
                "detail": str(detail),
                "usage": dict(usage),
            },
        )

    def append_tool_started(
        self,
        call_id,
        *,
        effect_scope,
        potential_effects,
        operation,
    ):
        return self.append(
            "tool_started",
            {
                "call_id": str(call_id),
                "effect_scope": str(effect_scope),
                "potential_effects": list(potential_effects),
                "operation": dict(operation),
            },
        )

    def append_tool_result(
        self,
        outcome,
        *,
        recovered_from_interruption=False,
    ):
        payload = {
            "outcome": outcome.to_dict(),
        }
        if recovered_from_interruption:
            payload["recovered_from_interruption"] = True
        return self.append(
            "tool_result",
            payload,
        )

    def append_stopped(self, content, stop_reason, *, attempt_duration_ms=0):
        self._require_ready()
        payload = {
            "content": str(content),
            "stop_reason": str(stop_reason),
            "attempt_duration_ms": int(attempt_duration_ms),
        }
        return self.append("run_stopped", payload)

    def _require_ready(self):
        if self.projection.phase != "ready_for_model":
            raise RuntimeError("Run must reach a ready-for-model boundary first")

    def append_compaction(self, context):
        if not isinstance(context, CompactedContext):
            raise TypeError("compaction requires a CompactedContext")
        self._require_ready()
        active = self.history().recent_events()
        covered = tuple(
            entry
            for entry in active
            if entry.sequence <= context.covered_through_sequence
        )
        if not covered or covered != active[: len(covered)]:
            raise ValueError("compaction coverage must be the exact active prefix")
        remaining = active[len(covered) :]
        if remaining and remaining[0].kind == "tool_result":
            raise ValueError("compaction cannot split an Assistant turn")
        return self.append("compaction", {"context": context.to_dict()})
