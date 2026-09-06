"""Single durable event log for one Pico run."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .contracts import EFFECT_SCOPES, ToolCall, ToolOutcome
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
        "tool_started",
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


def _validate_user_payload(kind, payload):
    _exact_payload(kind, payload, {"contract"})
    TaskContract.from_dict(payload["contract"])


def _validate_tool_call_payload(kind, payload):
    _exact_payload(kind, payload, {"name", "args", "call_id"})
    if not isinstance(payload["call_id"], str) or not payload["call_id"].strip():
        raise ValueError("assistant_tool_call requires a call id")
    ToolCall(str(payload["name"]), payload["args"], str(payload["call_id"]))


def _batch_calls(payload):
    return tuple(
        ToolCall(str(item["name"]), item["args"], str(item["call_id"]))
        for item in payload["calls"]
    )


def _validate_tool_batch_payload(kind, payload):
    _exact_payload(kind, payload, {"batch_id", "calls"})
    if not isinstance(payload["batch_id"], str) or not payload["batch_id"].strip():
        raise ValueError("assistant_tool_batch requires a batch id")
    if not isinstance(payload["calls"], list) or len(payload["calls"]) < 2:
        raise ValueError("assistant_tool_batch requires at least two calls")
    for item in payload["calls"]:
        if not isinstance(item, dict) or set(item) != {"name", "args", "call_id"}:
            raise ValueError("assistant_tool_batch has an invalid call")
        if not isinstance(item["call_id"], str) or not item["call_id"].strip():
            raise ValueError("assistant_tool_batch calls require call ids")
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise ValueError("assistant_tool_batch calls require tool names")
    calls = _batch_calls(payload)
    call_ids = tuple(call.call_id for call in calls)
    if len(set(call_ids)) != len(call_ids):
        raise ValueError("assistant_tool_batch call ids must be unique")


def _validate_tool_started_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {
            "tool_call_id",
            "tool_name",
            "effect_scope",
            "potential_effects",
        },
    )
    if not str(payload["tool_call_id"]) or not str(payload["tool_name"]):
        raise ValueError("tool_started requires call and tool names")
    if payload["effect_scope"] not in EFFECT_SCOPES:
        raise ValueError("tool_started has invalid effect scope")
    if not isinstance(payload["potential_effects"], list):
        raise TypeError("tool_started has invalid field types")
    for effect in payload["potential_effects"]:
        if not isinstance(effect, dict) or set(effect) != {
            "path",
            "before_state",
            "before_artifact_id",
        }:
            raise ValueError("tool_started has invalid potential effect")


def _validate_tool_result_payload(kind, payload):
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
        raise TypeError("tool_result recovery marker must be boolean")


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
    "model_instruction": _validate_text_payload,
    "assistant_tool_call": _validate_tool_call_payload,
    "assistant_tool_batch": _validate_tool_batch_payload,
    "tool_started": _validate_tool_started_payload,
    "tool_result": _validate_tool_result_payload,
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
    task_id: str
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
            "task_id": self.task_id,
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
            "task_id",
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
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            kind=str(value["kind"]),
            timestamp=str(value["timestamp"]),
            payload=dict(value["payload"]),
        )

    @property
    def content(self):
        if self.kind == "user_message":
            return str(dict(self.payload.get("contract", {})).get("goal", ""))
        if self.kind in {"user_guidance", "model_instruction", "assistant_final"}:
            return str(self.payload.get("content", ""))
        if self.kind == "tool_result":
            outcome = dict(self.payload.get("outcome", {}) or {})
            return str(outcome.get("content", ""))
        if self.kind == "compaction":
            return str(self.payload.get("content", ""))
        return ""

    @property
    def name(self):
        if self.kind == "assistant_tool_call":
            return str(self.payload.get("name", ""))
        if self.kind == "tool_result":
            return str(dict(self.payload.get("outcome", {}) or {}).get("tool_name", ""))
        return ""

    @property
    def batch_id(self):
        return str(self.payload.get("batch_id", ""))

    @property
    def batch_calls(self):
        if self.kind != "assistant_tool_batch":
            return ()
        return _batch_calls(self.payload)

    @property
    def args(self):
        return dict(self.payload.get("args", {}) or {})

    @property
    def call_id(self):
        if self.kind == "assistant_tool_call":
            return str(self.payload.get("call_id", ""))
        if self.kind == "tool_result":
            return str(
                dict(self.payload.get("outcome", {}) or {}).get("tool_call_id", "")
            )
        return str(self.payload.get("tool_call_id", ""))

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

    def __init__(self, run_id, task_id, session_id, store):
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.session_id = str(session_id)
        self.store = store
        self._events = []
        self.projection = RunProjection()


    @property
    def generation(self):
        return 1 + sum(event.kind == "compaction" for event in self._events)

    @classmethod
    def _from_events(cls, events, store, *, expected_run_id):
        """Restore the writer and projection from one storage snapshot."""
        events = tuple(events)
        if not events:
            raise ValueError("active Run Log is missing or empty")
        projection = replay_events(events, expected_run_id=expected_run_id)
        first = events[0]
        log = cls(first.run_id, first.task_id, first.session_id, store)
        log._events = list(events)
        log.projection = projection
        return log, projection

    @property
    def events(self):
        return tuple(self._events)

    def append(self, kind, payload=None):
        sequence = len(self._events) + 1
        entry = RunEvent(
            event_id=f"{self.run_id}:event:{sequence:06d}",
            sequence=sequence,
            run_id=self.run_id,
            task_id=self.task_id,
            session_id=self.session_id,
            kind=str(kind),
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=dict(payload or {}),
        )
        self.projection.check_event(entry)
        self.store._append_event(entry)
        self._events.append(entry)
        self.projection._advance_event(entry)
        return entry

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

    def append_tool_call(self, call):
        return self.append(
            "assistant_tool_call",
            {"name": call.name, "args": dict(call.args), "call_id": call.call_id},
        )

    def append_tool_batch(self, calls):
        calls = tuple(calls)
        if len(calls) < 2:
            raise ValueError("tool batch requires at least two calls")
        batch_id = "batch_" + calls[0].call_id
        return self.append(
            "assistant_tool_batch",
            {
                "batch_id": batch_id,
                "calls": [
                    {
                        "name": call.name,
                        "args": dict(call.args),
                        "call_id": call.call_id,
                    }
                    for call in calls
                ],
            },
        )

    def append_tool_started(
        self,
        call,
        *,
        effect_scope,
        potential_effects,
    ):
        return self.append(
            "tool_started",
            {
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                "effect_scope": str(effect_scope),
                "potential_effects": list(potential_effects),
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

    def append_model_instruction(self, content):
        self._require_no_pending()
        return self.append("model_instruction", {"content": str(content)})

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
        return self.projection.pending_call_id or ""

    def pending_batch_id(self):
        return self.projection.pending_batch_id

    def pending_tool_calls(self):
        return tuple(self.projection.pending_calls[self.projection.result_count :])

    def pending_tool_call(self):
        pending_calls = self.pending_tool_calls()
        if not pending_calls:
            return None
        if self.pending_batch_id():
            raise RuntimeError("pending tool transaction is an observation batch")
        return pending_calls[0]


    def _require_no_pending(self):
        if self.pending_tool_calls():
            raise RuntimeError("pending tool calls must receive results first")


    def append_compaction(self, content, covered_event_ids):
        covered = tuple(covered_event_ids)
        if not covered or len(set(covered)) != len(covered):
            raise ValueError("compaction must cover a non-empty unique prefix")
        active = RunHistory(self._events).active_events()
        if covered != tuple(entry.event_id for entry in active[: len(covered)]):
            raise ValueError("compaction coverage must be the exact active prefix")
        remaining = active[len(covered) :]
        if remaining and remaining[0].kind == "tool_result":
            raise ValueError("compaction cannot split a tool call/result batch")
        return self.append(
            "compaction",
            {
                "content": content,
                "covered_event_ids": list(covered),
            },
        )
