"""Single durable event log for one Pico run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import EFFECT_SCOPES, FailureInfo, ToolCall, ToolOutcome
from .delivery import FinalDiffDescriptor
from .run_projection import RunProjection
from .task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskContract

COMPACTED_HISTORY_OMITTED = "- recent events omitted by History budget"
CONTEXT_KINDS = frozenset(
    {
        "user_message",
        "user_guidance",
        "assistant_tool_call",
        "assistant_tool_batch",
        "tool_result",
        "model_instruction",
        "assistant_final",
        "compaction",
    }
)
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
        {"content", "stop_reason", "run_duration_ms", "final_diff"},
    )
    if not str(payload["content"]).strip():
        raise ValueError("assistant_final requires content")
    if payload["stop_reason"] != STOP_REASON_FINAL_ANSWER_RETURNED:
        raise ValueError("assistant_final has invalid stop reason")
    if int(payload["run_duration_ms"]) < 0:
        raise ValueError("assistant_final duration cannot be negative")
    final_diff = FinalDiffDescriptor.from_dict(payload["final_diff"])
    if final_diff.unavailable_reason:
        raise ValueError("assistant_final requires an available final Diff")


def _validate_stopped_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"content", "stop_reason", "run_duration_ms", "final_diff"},
    )
    if not str(payload["stop_reason"]):
        raise ValueError("run_stopped requires stop_reason")
    if int(payload["run_duration_ms"]) < 0:
        raise ValueError("run_stopped duration cannot be negative")
    FinalDiffDescriptor.from_dict(payload["final_diff"])


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
        },
        {
            "command",
            "exit_code",
            "output",
        },
    )
    if payload["status"] not in {"passed", "failed", "infrastructure_error"}:
        raise ValueError("verification_result has invalid status")
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


class _RunProtocol:
    def __init__(self):
        self.has_user_message = False
        self.pending_calls = ()
        self.pending_batch_id = ""
        self.started_call_ids = set()
        self.last_started_ordinal = -1
        self.result_count = 0
        self.terminal = False

    @property
    def pending_call_id(self):
        return self.pending_calls[0].call_id if len(self.pending_calls) == 1 else ""

    def _pending_call(self, call_id):
        return next(
            (call for call in self.pending_calls if call.call_id == call_id),
            None,
        )

    def _begin_calls(self, calls, *, batch_id=""):
        self.pending_calls = tuple(calls)
        self.pending_batch_id = str(batch_id)
        self.started_call_ids = set()
        self.last_started_ordinal = -1
        self.result_count = 0

    def _clear_calls(self):
        self._begin_calls(())

    def check(self, kind, payload):  # noqa: C901 - linear protocol state machine
        if self.terminal:
            raise ValueError("Run Log cannot append after a terminal event")
        if not self.has_user_message and kind != "user_message":
            raise ValueError("Run Log must begin with user_message")
        if kind == "user_message" and self.has_user_message:
            raise ValueError("Run Log may contain only one user_message")
        if kind in {"assistant_tool_call", "assistant_tool_batch"}:
            if self.pending_calls:
                raise ValueError("Run Log already has pending tool calls")
        elif kind == "tool_started":
            call_id = str(payload["tool_call_id"])
            call = self._pending_call(call_id)
            if call is None:
                raise ValueError("tool_started must match a pending tool call")
            if str(payload["tool_name"]) != call.name:
                raise ValueError("tool_started tool name does not match the pending call")
            if self.result_count:
                raise ValueError("tool_started cannot follow a batch result")
            if call_id in self.started_call_ids:
                raise ValueError("pending tool call already started")
            ordinal = self.pending_calls.index(call)
            if ordinal <= self.last_started_ordinal:
                raise ValueError("tool_started calls must preserve batch order")
        elif kind == "tool_result":
            outcome = ToolOutcome.from_dict(payload["outcome"])
            call_id = outcome.tool_call_id
            if not self.pending_calls or self.result_count >= len(self.pending_calls):
                raise ValueError("tool_result requires pending tool calls")
            call = self.pending_calls[self.result_count]
            if call_id != call.call_id:
                raise ValueError("tool_result calls must preserve batch order")
            if outcome.tool_name != call.name:
                raise ValueError("tool_result tool name does not match the pending call")
            started = call_id in self.started_call_ids
            if outcome.execution_state == "not_started" and started:
                raise ValueError("started tool cannot finish as not_started")
            if outcome.execution_state != "not_started" and not started:
                raise ValueError("executed tool_result requires tool_started")
        elif self.pending_calls and kind not in {"tool_started", "tool_result"}:
            raise ValueError("pending tool calls must receive results first")

    def apply(self, entry):
        self.check(entry.kind, entry.payload)
        if entry.kind == "user_message":
            self.has_user_message = True
        elif entry.kind == "assistant_tool_call":
            self._begin_calls((ToolCall(entry.name, entry.args, entry.call_id),))
        elif entry.kind == "assistant_tool_batch":
            self._begin_calls(_batch_calls(entry.payload), batch_id=entry.batch_id)
        elif entry.kind == "tool_started":
            call = self._pending_call(entry.call_id)
            self.started_call_ids.add(entry.call_id)
            self.last_started_ordinal = self.pending_calls.index(call)
        elif entry.kind == "tool_result":
            self.result_count += 1
            if self.result_count == len(self.pending_calls):
                self._clear_calls()
        elif entry.kind in {"assistant_final", "run_stopped"}:
            self.terminal = True


def _validate_event_identity(events, *, expected_run_id=None):
    if not events:
        return
    first = events[0]
    run_id = first.run_id
    if expected_run_id is not None and run_id != str(expected_run_id):
        raise ValueError("Run event belongs to another run")
    for expected_sequence, entry in enumerate(events, start=1):
        if entry.sequence != expected_sequence:
            raise ValueError("Run Log sequence is not contiguous")
        if entry.event_id != f"{run_id}:event:{expected_sequence:06d}":
            raise ValueError("Run event id does not match its sequence")
        if (
            entry.run_id != run_id
            or entry.task_id != first.task_id
            or entry.session_id != first.session_id
        ):
            raise ValueError("Run event identity changed within one run")


def validate_run_events(events, *, expected_run_id=None):
    events = tuple(events)
    _validate_event_identity(events, expected_run_id=expected_run_id)
    protocol = _RunProtocol()
    for entry in events:
        protocol.apply(entry)
    return protocol


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


def replay_events(events):
    events = tuple(events)
    validate_run_events(events)
    projection = RunProjection()
    for event in events:
        projection.apply_event(event)
    return projection


class RunLog:
    """Append-only Run facts plus the model-visible context projection."""

    def __init__(self, run_id, task_id, session_id, store, events=()):
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.session_id = str(session_id)
        self.store = store
        self._events = list(events)
        self._protocol = validate_run_events(self._events)
        compactions = [entry for entry in self._events if entry.kind == "compaction"]
        self.generation = len(compactions) + 1
        self.reconciled_outcomes = []

    @classmethod
    def restore(cls, run_id, store):
        events = store.read_events(run_id)
        if not events:
            raise ValueError("active Run Log is missing or empty")
        first = events[0]
        return cls(first.run_id, first.task_id, first.session_id, store, events)

    @property
    def events(self):
        return tuple(self._events)

    def append(self, kind, payload=None):
        payload = payload or {}
        _validate_event_payload(kind, payload)
        self._protocol.check(kind, payload)
        entry = self.store.append_event(
            self.run_id,
            self.task_id,
            self.session_id,
            kind,
            payload,
            protocol_checked=True,
        )
        self._events.append(entry)
        self._protocol.apply(entry)
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

    def append_final(self, content, final_diff, *, run_duration_ms=0):
        self._require_no_pending()
        if not isinstance(final_diff, FinalDiffDescriptor):
            raise TypeError("assistant_final requires a FinalDiffDescriptor")
        return self.append(
            "assistant_final",
            {
                "content": str(content),
                "stop_reason": STOP_REASON_FINAL_ANSWER_RETURNED,
                "run_duration_ms": int(run_duration_ms),
                "final_diff": final_diff.to_dict(),
            },
        )

    def append_stopped(self, content, stop_reason, final_diff, *, run_duration_ms=0):
        self._require_no_pending()
        if not isinstance(final_diff, FinalDiffDescriptor):
            raise TypeError("run_stopped requires a FinalDiffDescriptor")
        return self.append(
            "run_stopped",
            {
                "content": str(content),
                "stop_reason": str(stop_reason),
                "run_duration_ms": int(run_duration_ms),
                "final_diff": final_diff.to_dict(),
            },
        )

    def pending_call_id(self):
        return self._protocol.pending_call_id

    def pending_batch_id(self):
        return self._protocol.pending_batch_id

    def pending_tool_calls(self):
        return tuple(self._protocol.pending_calls[self._protocol.result_count :])

    def pending_tool_call(self):
        pending_calls = self.pending_tool_calls()
        if not pending_calls:
            return None
        if self.pending_batch_id():
            raise RuntimeError("pending tool transaction is an observation batch")
        return pending_calls[0]

    def latest_user_guidance(self):
        entry = next(
            (
                candidate
                for candidate in reversed(self.active_events())
                if candidate.kind == "user_guidance"
            ),
            None,
        )
        return entry.content if entry is not None else ""

    def _require_no_pending(self):
        if self.pending_tool_calls():
            raise RuntimeError("pending tool calls must receive results first")

    def reconcile_interrupted(self, runtime):
        pending_calls = self.pending_tool_calls()
        if not pending_calls:
            return ()
        started_by_id = {
            entry.call_id: entry
            for entry in self._events
            if entry.kind == "tool_started"
        }
        for call in pending_calls:
            started = started_by_id.get(call.call_id)
            if started is None:
                detail = "tool call was persisted but never entered execution"
                outcome = ToolOutcome(
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    status="error",
                    execution_state="not_started",
                    side_effect_state="none",
                    content=detail,
                    failure=FailureInfo(
                        "operation_not_started",
                        detail,
                        "retry_after_wait",
                    ),
                )
            else:
                potential = list(started.payload.get("potential_effects", []))
                changed = []
                transitions = []
                for effect in potential:
                    logical = str(effect.get("path", ""))
                    if not logical:
                        continue
                    path = Path(logical)
                    if not path.is_absolute():
                        path = runtime.workspace.resolve_path(logical)
                    before = str(effect.get("before_state", ""))
                    before_artifact_id = str(effect.get("before_artifact_id", ""))
                    after = runtime.workspace.path_state(path)
                    if before != after:
                        changed.append(logical)
                        transitions.append(
                            {
                                "path": logical,
                                "before_state": before,
                                "after_state": after,
                                "before_artifact_id": before_artifact_id,
                            }
                        )
                effect_scope = str(started.payload.get("effect_scope", "none"))
                unknown = effect_scope == "workspace" and not potential
                uncertain = bool(changed or unknown)
                detail = "tool execution was interrupted before a durable result"
                outcome = ToolOutcome(
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    status="partial_success" if uncertain else "error",
                    execution_state="failed",
                    side_effect_state=(
                        "partial" if changed else ("unknown" if unknown else "none")
                    ),
                    content=detail,
                    failure=FailureInfo(
                        "operation_interrupted",
                        detail,
                        "no_retry" if uncertain else "retry_after_wait",
                    ),
                    affected_paths=tuple(changed),
                    effect_scope=effect_scope if changed or unknown else "none",
                    structured={"path_transitions": transitions},
                )
            entry = self.append_tool_result(
                outcome,
                recovered_from_interruption=True,
            )
            self.reconciled_outcomes.append((outcome, entry))
        return tuple(self.reconciled_outcomes)

    def context_events(self):
        calls = {
            call_id
            for entry in self._events
            for call_id in (
                (entry.call_id,)
                if entry.kind == "assistant_tool_call"
                else tuple(call.call_id for call in entry.batch_calls)
            )
            if entry.kind in {"assistant_tool_call", "assistant_tool_batch"}
            and call_id
        }
        return tuple(
            entry
            for entry in self._events
            if entry.kind in CONTEXT_KINDS
            and (entry.kind != "tool_result" or entry.call_id in calls)
        )

    def active_events(self):
        active = []
        for entry in self.context_events():
            if entry.kind != "compaction":
                active.append(entry)
                continue
            covered = entry.covered_event_ids
            prefix = tuple(item.event_id for item in active[: len(covered)])
            if not covered or prefix != covered:
                raise ValueError(
                    "compaction coverage must match the active logical prefix"
                )
            active = [entry, *active[len(covered) :]]
        return tuple(active)

    @staticmethod
    def _history_units(events, *, allow_incomplete=False):
        units = []
        index = 0
        events = tuple(events)
        while index < len(events):
            entry = events[index]
            if entry.kind == "assistant_tool_call":
                expected_ids = (entry.call_id,)
            elif entry.kind == "assistant_tool_batch":
                expected_ids = tuple(call.call_id for call in entry.batch_calls)
            else:
                if entry.kind == "tool_result":
                    raise RuntimeError("Run Log contains an orphan tool result")
                units.append((entry,))
                index += 1
                continue
            end = index + 1 + len(expected_ids)
            if end > len(events):
                if allow_incomplete:
                    return None
                raise RuntimeError("Run Log contains an incomplete tool transaction")
            results = events[index + 1 : end]
            if tuple(result.call_id for result in results) != expected_ids or any(
                result.kind != "tool_result" for result in results
            ):
                raise RuntimeError("Run Log tool transaction is not contiguous")
            units.append((entry, *results))
            index = end
        return units

    def compact(self, *, retain_tokens, history_token_counter, summary_builder):
        active = list(self.active_events())
        latest_guidance_id = self._latest_user_guidance_id(active)
        units = self._history_units(active, allow_incomplete=True)
        if units is None:
            return None

        def render(candidate_units, *, summary=""):
            events = tuple(
                event for unit in candidate_units for event in unit
            )
            visible = self._without_projected_state(
                events,
                projected_guidance_id=latest_guidance_id,
            )
            lines = ["Current run events:"]
            if summary:
                lines.append(f"[compaction] {summary}")
            lines.extend(self._render_event(event) for event in visible)
            if len(lines) == 1:
                lines.append("- empty")
            return "\n".join(lines)

        retained = []
        limit = max(1, int(retain_tokens))
        for unit in reversed(units):
            candidate = [unit, *retained]
            candidate_tokens = max(
                1,
                int(history_token_counter(render(candidate))),
            )
            if retained and candidate_tokens > limit:
                break
            retained = candidate
        cut = max(0, len(units) - len(retained))
        guidance_unit_index = next(
            (
                index
                for index, unit in enumerate(units)
                if any(event.event_id == latest_guidance_id for event in unit)
            ),
            None,
        )
        if guidance_unit_index is not None and cut > guidance_unit_index:
            cut = guidance_unit_index
            retained = units[cut:]
        retained_tokens = max(
            1,
            int(history_token_counter(render(retained))),
        )
        compacted = tuple(item for unit in units[:cut] for item in unit)
        if not compacted:
            return None
        summary_events = tuple(
            self._without_projected_state(
                compacted,
                projected_guidance_id=latest_guidance_id,
            )
        )
        if not summary_events:
            return None
        summary = summary_builder(summary_events)
        before = render(units)
        after = render(retained, summary=summary)
        if history_token_counter(after) >= history_token_counter(before):
            return None
        event = self._commit_compaction(
            summary,
            [entry.event_id for entry in compacted],
        )
        return event, {
            "mode": "semantic_history",
            "covered_events": len(compacted),
            "retained_events": sum(len(unit) for unit in units[cut:]),
            "retained_tokens": retained_tokens,
            "summary_tokens": history_token_counter(render((), summary=summary)),
        }

    def _commit_compaction(self, content, covered_event_ids):
        covered = tuple(covered_event_ids)
        if not covered or len(set(covered)) != len(covered):
            raise ValueError("compaction must cover a non-empty unique prefix")
        active = self.active_events()
        if covered != tuple(entry.event_id for entry in active[: len(covered)]):
            raise ValueError("compaction coverage must be the exact active prefix")
        remaining = active[len(covered) :]
        if remaining and remaining[0].kind == "tool_result":
            raise ValueError("compaction cannot split a tool call/result batch")
        self.generation += 1
        return self.append(
            "compaction",
            {
                "content": content,
                "covered_event_ids": list(covered),
            },
        )

    @staticmethod
    def _render_event(entry):
        if entry.kind == "assistant_tool_call":
            return (
                f"[assistant/tool] {entry.name} "
                + json.dumps(entry.args or {}, ensure_ascii=False, sort_keys=True)
            )
        if entry.kind == "assistant_tool_batch":
            calls = [
                {"call_id": call.call_id, "name": call.name, "args": call.args}
                for call in entry.batch_calls
            ]
            return (
                f"[assistant/tool_batch/{entry.batch_id}] "
                + json.dumps(calls, ensure_ascii=False, sort_keys=True)
            )
        if entry.kind == "tool_result":
            artifact = f" artifact={entry.artifact_id}" if entry.artifact_id else ""
            outcome = ToolOutcome.from_dict(entry.payload["outcome"])
            return (
                f"[tool/{entry.name}/{entry.outcome_status}/"
                f"{entry.side_effect_state}{artifact}] {outcome.render_for_model()}"
            )
        return f"[{entry.kind}] {entry.content}"

    @staticmethod
    def _latest_user_guidance_id(events):
        return next(
            (
                entry.event_id
                for entry in reversed(tuple(events))
                if entry.kind == "user_guidance"
            ),
            "",
        )

    @staticmethod
    def _without_projected_state(events, *, projected_guidance_id=""):
        selected = []
        index = 0
        while index < len(events):
            entry = events[index]
            if entry.kind == "user_message":
                index += 1
                continue
            if entry.kind == "user_guidance" and entry.event_id == projected_guidance_id:
                index += 1
                continue
            if (
                entry.kind == "assistant_tool_call"
                and entry.name == "update_working_state"
                and index + 1 < len(events)
            ):
                result = events[index + 1]
                if (
                    result.kind == "tool_result"
                    and result.call_id == entry.call_id
                    and result.outcome_status == "success"
                ):
                    index += 2
                    continue
            selected.append(entry)
            index += 1
        return selected

    def render_projection(self):
        active = self.active_events()
        selected = self._without_projected_state(
            active,
            projected_guidance_id=self._latest_user_guidance_id(active),
        )
        lines = ["Current run events:"]
        artifact_references = 0
        for entry in selected:
            artifact_references += bool(entry.artifact_id)
            lines.append(self._render_event(entry))
        if len(lines) == 1:
            lines.append("- empty")
        return "\n".join(lines), {
            "active_count": len(active),
            "selected_count": len(selected),
            "omitted_count": max(0, len(active) - len(selected)),
            "artifact_references": artifact_references,
        }

    def render_compacted_projection(self, *, retain_tokens, token_counter):
        """Render complete summaries followed by a complete recent-event suffix."""

        active = self.active_events()
        selected = self._without_projected_state(
            active,
            projected_guidance_id=self._latest_user_guidance_id(active),
        )
        summaries = tuple(entry for entry in selected if entry.kind == "compaction")
        if not summaries:
            return None
        recent = tuple(entry for entry in selected if entry.kind != "compaction")
        units = self._history_units(recent)

        limit = max(0, int(retain_tokens))

        def render(candidate):
            retained_count = sum(len(unit) for unit in candidate)
            lines = ["Current run events:"]
            lines.extend(self._render_event(entry) for entry in summaries)
            if retained_count < len(recent):
                lines.append(COMPACTED_HISTORY_OMITTED)
            for unit in candidate:
                lines.extend(self._render_event(entry) for entry in unit)
            return "\n".join(lines)

        retained = []
        minimum = render(retained)
        if token_counter(minimum) > limit:
            raise ValueError(
                "committed compaction summary exceeds the History budget"
            )
        for unit in reversed(units):
            candidate = [unit, *retained]
            if retained and token_counter(render(candidate)) > limit:
                break
            retained = candidate
        text = render(retained)
        flattened = tuple(entry for unit in retained for entry in unit)
        return text, {
            "active_count": len(active),
            "selected_count": len(summaries) + len(flattened),
            "omitted_count": max(
                0,
                len(active) - len(summaries) - len(flattened),
            ),
            "artifact_references": sum(
                bool(entry.artifact_id) for entry in (*summaries, *flattened)
            ),
            "projection_mode": "compacted_complete_transactions",
            "retained_tokens": token_counter(text),
        }

    def render_recent_projection(self, *, retain_tokens, token_counter):
        """Render a bounded suffix without splitting a Tool transaction."""

        active = self.active_events()
        selected = self._without_projected_state(
            active,
            projected_guidance_id=self._latest_user_guidance_id(active),
        )
        units = self._history_units(selected, allow_incomplete=True) or []

        limit = max(0, int(retain_tokens))
        retained = []

        def render(candidate):
            retained_count = sum(len(unit) for unit in candidate)
            omitted = max(0, len(selected) - retained_count)
            lines = ["Current run events (bounded fallback):"]
            lines.append(f"- {omitted} older events omitted")
            for unit in candidate:
                lines.extend(self._render_event(entry) for entry in unit)
            return "\n".join(lines)

        for unit in reversed(units):
            candidate = [unit, *retained]
            if retained and token_counter(render(candidate)) > limit:
                break
            retained = candidate
        text = render(retained)
        flattened = tuple(entry for unit in retained for entry in unit)
        return text, {
            "active_count": len(active),
            "selected_count": len(flattened),
            "omitted_count": max(0, len(active) - len(flattened)),
            "artifact_references": sum(
                bool(entry.artifact_id) for entry in flattened
            ),
            "retained_tokens": token_counter(text),
        }
