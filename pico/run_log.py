"""Single durable event log for one Pico run."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .contracts import EFFECT_SCOPES, TOOL_ARTIFACT_ID, ToolCall, ToolOutcome
from .delivery import FinalDiff
from .history import CONTEXT_KINDS, RunHistory
from .run_projection import RunProjection
from .task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskContract

RUN_EVENT_KINDS = frozenset(
    {
        *CONTEXT_KINDS,
        "run_started",
        "run_resumed",
        "model_requested",
        "turn_metrics",
        "completion_blocked",
        "tool_exchange",
        "tool_intent",
        "tool_settlement",
        "verification_result",
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


def _validate_model_instruction_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"instruction", "evidence", "evidence_artifact_id"},
    )
    if (
        not isinstance(payload["instruction"], str)
        or not payload["instruction"].strip()
    ):
        raise ValueError("model_instruction requires trusted instruction text")
    if not isinstance(payload["evidence"], str):
        raise TypeError("model_instruction evidence must be text")
    artifact_id = payload["evidence_artifact_id"]
    if not isinstance(artifact_id, str) or (
        artifact_id and not TOOL_ARTIFACT_ID.fullmatch(artifact_id)
    ):
        raise ValueError("model_instruction evidence artifact is invalid")


def _validate_completion_blocked_payload(kind, payload):
    _exact_payload(kind, payload, {"status"})
    if not isinstance(payload["status"], str) or not payload["status"].strip():
        raise ValueError("completion_blocked requires a status")


def _validate_user_payload(kind, payload):
    _exact_payload(kind, payload, {"contract"})
    TaskContract.from_dict(payload["contract"])


def _tool_call(payload):
    return ToolCall(
        str(payload["name"]),
        payload["args"],
        str(payload["call_id"]),
    )


def _validate_call(kind, payload):
    _exact_payload(kind, payload, {"name", "args", "call_id"})
    if not isinstance(payload["call_id"], str) or not payload["call_id"].strip():
        raise ValueError(f"{kind} requires a call id")
    if not isinstance(payload["name"], str) or not payload["name"].strip():
        raise ValueError(f"{kind} requires a tool name")
    _tool_call(payload)


def _validate_tool_exchange_payload(kind, payload):
    _exact_payload(kind, payload, {"call", "outcome"})
    call = _tool_call(payload["call"])
    outcome = ToolOutcome.from_dict(payload["outcome"])
    if (outcome.tool_call_id, outcome.tool_name) != (call.call_id, call.name):
        raise ValueError("tool_exchange call and outcome do not match")
    if outcome.side_effect_state != "none" or outcome.effect_scope != "none":
        raise ValueError("tool_exchange cannot contain workspace side effects")


def _validate_tool_intent_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {
            "call",
            "effect_scope",
            "potential_effects",
            "operation",
        },
    )
    _validate_call("tool_intent call", payload["call"])
    if payload["effect_scope"] not in EFFECT_SCOPES - {"none"}:
        raise ValueError("tool_intent requires a non-empty effect scope")
    if not isinstance(payload["potential_effects"], list) or not isinstance(
        payload["operation"], dict
    ):
        raise TypeError("tool_intent has invalid field types")
    for effect in payload["potential_effects"]:
        if not isinstance(effect, dict) or set(effect) != {
            "path",
            "before_state",
            "before_artifact_id",
        }:
            raise ValueError("tool_intent has invalid potential effect")


def _validate_tool_settlement_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"outcome"},
        {"recovered_from_interruption"},
    )
    ToolOutcome.from_dict(payload["outcome"])
    if "recovered_from_interruption" in payload and not isinstance(
        payload["recovered_from_interruption"], bool
    ):
        raise TypeError("tool_settlement recovery marker must be boolean")


def _validate_final_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"content", "stop_reason", "turn_duration_ms", "final_diff"},
    )
    if not str(payload["content"]).strip():
        raise ValueError("assistant_final requires content")
    if payload["stop_reason"] != STOP_REASON_FINAL_ANSWER_RETURNED:
        raise ValueError("assistant_final has invalid stop reason")
    if int(payload["turn_duration_ms"]) < 0:
        raise ValueError("assistant_final duration cannot be negative")
    FinalDiff.from_dict(payload["final_diff"])


def _validate_stopped_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"content", "stop_reason", "turn_duration_ms"},
        {"final_diff"},
    )
    if not str(payload["stop_reason"]):
        raise ValueError("run_stopped requires stop_reason")
    if int(payload["turn_duration_ms"]) < 0:
        raise ValueError("run_stopped duration cannot be negative")
    if "final_diff" in payload:
        FinalDiff.from_dict(payload["final_diff"])


def _validate_verification_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {
            "status",
            "started_workspace_mutation_sequence",
            "finished_workspace_mutation_sequence",
            "started_changed_path_states",
            "finished_changed_path_states",
            "workspace_changes",
        },
        {
            "command",
            "exit_code",
            "output",
        },
    )
    if payload["status"] not in {"passed", "failed", "infrastructure_error"}:
        raise ValueError("verification_result has invalid status")
    changes = payload["workspace_changes"]
    if changes is not None and (
        not isinstance(changes, list)
        or any(not isinstance(path, str) or not path for path in changes)
    ):
        raise TypeError("verification workspace_changes must be paths or null")
    if (changes is None or changes) and payload["status"] == "passed":
        raise ValueError("verification with uncertain workspace effects cannot pass")
    if not isinstance(payload["finished_workspace_mutation_sequence"], int):
        raise TypeError(
            "verification_result finished mutation sequence must be an integer"
        )
    if not isinstance(payload["started_workspace_mutation_sequence"], int):
        raise TypeError(
            "verification_result started mutation sequence must be an integer"
        )
    for state_field in (
        "started_changed_path_states",
        "finished_changed_path_states",
    ):
        states = payload[state_field]
        if not isinstance(states, dict) or any(
            not isinstance(path, str)
            or not path
            or not isinstance(state, str)
            for path, state in states.items()
        ):
            raise TypeError(
                f"verification_result {state_field} must map paths to states"
            )


_PAYLOAD_VALIDATORS = {
    "user_message": _validate_user_payload,
    "user_guidance": _validate_text_payload,
    "model_instruction": _validate_model_instruction_payload,
    "completion_blocked": _validate_completion_blocked_payload,
    "tool_exchange": _validate_tool_exchange_payload,
    "tool_intent": _validate_tool_intent_payload,
    "tool_settlement": _validate_tool_settlement_payload,
    "verification_result": _validate_verification_payload,
    "assistant_final": _validate_final_payload,
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
        if self.kind == "model_instruction":
            return str(self.payload.get("instruction", ""))
        if self.kind in {"user_guidance", "assistant_final"}:
            return str(self.payload.get("content", ""))
        if self.kind in {"tool_exchange", "tool_settlement"}:
            outcome = dict(self.payload.get("outcome", {}) or {})
            return str(outcome.get("content", ""))
        if self.kind == "compaction":
            return str(self.payload.get("content", ""))
        return ""

    @property
    def name(self):
        if self.kind in {"tool_exchange", "tool_settlement"}:
            return str(dict(self.payload.get("outcome", {}) or {}).get("tool_name", ""))
        return ""

    @property
    def tool_call(self):
        if self.kind not in {"tool_exchange", "tool_intent"}:
            return None
        return _tool_call(self.payload["call"])

    @property
    def args(self):
        call = self.tool_call
        return dict(call.args) if call is not None else {}

    @property
    def call_id(self):
        if self.kind in {"tool_exchange", "tool_settlement"}:
            return str(
                dict(self.payload.get("outcome", {}) or {}).get("tool_call_id", "")
            )
        call = self.tool_call
        return call.call_id if call is not None else ""

    @property
    def outcome_status(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(outcome.get("status", ""))

    @property
    def side_effect_state(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(outcome.get("side_effect_state", ""))

    @property
    def affected_paths(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return tuple(str(item) for item in outcome.get("affected_paths", []))

    @property
    def artifact_id(self):
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(outcome.get("artifact_id", ""))

    @property
    def covered_event_ids(self):
        return tuple(str(item) for item in self.payload.get("covered_event_ids", []))


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
        self._events = []
        self.projection = RunProjection()


    @classmethod
    def _from_events(cls, events, store, *, expected_run_id):
        """Restore the writer and its Projection from one storage snapshot."""
        events = tuple(events)
        if not events:
            raise ValueError("active Run Log is missing or empty")
        projection = replay_events(events, expected_run_id=expected_run_id)
        first = events[0]
        log = cls(first.run_id, first.session_id, store)
        log._events = list(events)
        log.projection = projection
        return log

    @property
    def events(self):
        return tuple(self._events)

    def history(self):
        feedback = self.projection.runtime_feedback
        return RunHistory(
            self.events,
            projected_instruction_id=feedback.event_id if feedback else "",
        )

    def append(self, kind, payload=None):
        sequence = len(self._events) + 1
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
        self._events.append(entry)
        self.projection.__dict__.update(candidate.__dict__)
        if self.store.trace is not None:
            self.store.trace(entry)
        return entry

    def pending_tool_intent(self):
        for entry in reversed(self._events):
            if entry.kind == "tool_settlement":
                break
            if entry.kind == "tool_intent":
                return entry
        return None

    def append_user(self, contract):
        if not isinstance(contract, TaskContract):
            raise TypeError("user_message requires a TaskContract")
        return self.append("user_message", {"contract": contract.to_dict()})

    def append_user_guidance(self, content):
        content = str(content).strip()
        if not content:
            raise ValueError("user guidance must not be blank")
        self._require_no_pending()
        return self.append("user_guidance", {"content": content})

    @staticmethod
    def _call_payload(call):
        if not isinstance(call, ToolCall):
            raise TypeError("tool call must be a ToolCall")
        return {
            "name": call.name,
            "args": dict(call.args),
            "call_id": call.call_id,
        }

    def append_tool_exchange(self, call, outcome):
        if not isinstance(outcome, ToolOutcome):
            raise TypeError("tool exchange requires a ToolOutcome")
        return self.append(
            "tool_exchange",
            {
                "call": self._call_payload(call),
                "outcome": outcome.to_dict(),
            },
        )

    def append_tool_intent(
        self,
        call,
        *,
        effect_scope,
        potential_effects,
        operation,
    ):
        return self.append(
            "tool_intent",
            {
                "call": self._call_payload(call),
                "effect_scope": str(effect_scope),
                "potential_effects": list(potential_effects),
                "operation": dict(operation),
            },
        )

    def append_tool_settlement(
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
            "tool_settlement",
            payload,
        )

    def append_model_instruction(
        self,
        instruction,
        *,
        evidence="",
        evidence_artifact_id="",
    ):
        self._require_no_pending()
        return self.append(
            "model_instruction",
            {
                "instruction": str(instruction),
                "evidence": str(evidence),
                "evidence_artifact_id": str(evidence_artifact_id),
            },
        )

    def append_final(self, content, final_diff, *, turn_duration_ms=0):
        self._require_no_pending()
        if not isinstance(final_diff, FinalDiff):
            raise TypeError("assistant_final requires a FinalDiff")
        return self.append(
            "assistant_final",
            {
                "content": str(content),
                "stop_reason": STOP_REASON_FINAL_ANSWER_RETURNED,
                "turn_duration_ms": int(turn_duration_ms),
                "final_diff": final_diff.to_dict(),
            },
        )

    def append_stopped(self, content, stop_reason, final_diff=None, *, turn_duration_ms=0):
        self._require_no_pending()
        if final_diff is not None and not isinstance(final_diff, FinalDiff):
            raise TypeError("run_stopped final Diff must be FinalDiff or None")
        payload = {
            "content": str(content),
            "stop_reason": str(stop_reason),
            "turn_duration_ms": int(turn_duration_ms),
        }
        if final_diff is not None:
            payload["final_diff"] = final_diff.to_dict()
        return self.append("run_stopped", payload)

    def pending_call_id(self):
        return self.projection.pending_call_id

    def pending_tool_call(self):
        return self.projection.pending_tool.call

    def _require_no_pending(self):
        if self.pending_tool_call() is not None:
            raise RuntimeError("pending tool call must receive a result first")


    def append_compaction(self, content, covered_event_ids):
        covered = tuple(covered_event_ids)
        if not covered or len(set(covered)) != len(covered):
            raise ValueError("compaction must cover a non-empty unique prefix")
        active = self.history().active_events()
        if covered != tuple(entry.event_id for entry in active[: len(covered)]):
            raise ValueError("compaction coverage must be the exact active prefix")
        remaining = active[len(covered) :]
        if remaining and remaining[0].kind == "tool_settlement":
            raise ValueError("compaction cannot split a tool call/result pair")
        return self.append(
            "compaction",
            {
                "content": content,
                "covered_event_ids": list(covered),
            },
        )
