"""Single durable event log for one Pico run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import EFFECT_SCOPES, FailureInfo, ToolCall, ToolOutcome
from .features.memory import WorkingState
from .task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskState, apply_task_event

RUN_LOG_SCHEMA_VERSION = "run-log-v11"
CONTEXT_KINDS = frozenset(
    {
        "user_message",
        "assistant_tool_call",
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


def _validate_tool_call_payload(kind, payload):
    _exact_payload(kind, payload, {"name", "args", "call_id"})
    ToolCall(str(payload["name"]), payload["args"], str(payload["call_id"]))


def _validate_tool_started_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {
            "tool_call_id",
            "tool_name",
            "risky",
            "effect_scope",
            "potential_effects",
        },
    )
    if not str(payload["tool_call_id"]) or not str(payload["tool_name"]):
        raise ValueError("tool_started requires call and tool names")
    if payload["effect_scope"] not in EFFECT_SCOPES:
        raise ValueError("tool_started has invalid effect scope")
    if not isinstance(payload["risky"], bool) or not isinstance(
        payload["potential_effects"], list
    ):
        raise TypeError("tool_started has invalid field types")
    for effect in payload["potential_effects"]:
        if not isinstance(effect, dict) or set(effect) != {"path", "before_state"}:
            raise ValueError("tool_started has invalid potential effect")


def _validate_tool_result_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"tool_call_id", "tool_name", "workspace_revision", "outcome"},
        {"recovered_from_interruption"},
    )
    outcome = ToolOutcome.from_dict(payload["outcome"])
    if str(payload["tool_call_id"]) != outcome.tool_call_id:
        raise ValueError("tool_result call id does not match outcome")
    if str(payload["tool_name"]) != outcome.tool_name:
        raise ValueError("tool_result tool name does not match outcome")
    if not isinstance(payload["workspace_revision"], int):
        raise TypeError("tool_result workspace_revision must be an integer")
    if "recovered_from_interruption" in payload and not isinstance(
        payload["recovered_from_interruption"], bool
    ):
        raise TypeError("tool_result recovery marker must be boolean")


def _validate_final_payload(kind, payload):
    _exact_payload(kind, payload, {"content", "stop_reason", "run_duration_ms"})
    if not str(payload["content"]).strip():
        raise ValueError("assistant_final requires content")
    if payload["stop_reason"] != STOP_REASON_FINAL_ANSWER_RETURNED:
        raise ValueError("assistant_final has invalid stop reason")
    if int(payload["run_duration_ms"]) < 0:
        raise ValueError("assistant_final duration cannot be negative")


def _validate_stopped_payload(kind, payload):
    _exact_payload(kind, payload, {"content", "stop_reason", "run_duration_ms"})
    if not str(payload["stop_reason"]):
        raise ValueError("run_stopped requires stop_reason")
    if int(payload["run_duration_ms"]) < 0:
        raise ValueError("run_stopped duration cannot be negative")


def _validate_verification_payload(kind, payload):
    _exact_payload(
        kind,
        payload,
        {"status", "freshness", "finished_workspace_mutation_sequence"},
        {
            "command",
            "started_workspace_mutation_sequence",
            "exit_code",
            "output",
            "source_tool_call_id",
        },
    )
    if payload["freshness"] not in {"current", "stale"}:
        raise ValueError("verification_result has invalid freshness")
    if not isinstance(payload["finished_workspace_mutation_sequence"], int):
        raise TypeError(
            "verification_result finished mutation sequence must be an integer"
        )
    if "started_workspace_mutation_sequence" in payload and not isinstance(
        payload["started_workspace_mutation_sequence"], int
    ):
        raise TypeError(
            "verification_result started mutation sequence must be an integer"
        )


_PAYLOAD_VALIDATORS = {
    "user_message": _validate_text_payload,
    "model_instruction": _validate_text_payload,
    "assistant_tool_call": _validate_tool_call_payload,
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
        self.pending_call_id = ""
        self.pending_tool_name = ""
        self.started_call_id = ""
        self.terminal = False

    def check(self, kind, payload):  # noqa: C901 - linear protocol state machine
        if self.terminal:
            raise ValueError("Run Log cannot append after a terminal event")
        if not self.has_user_message and kind != "user_message":
            raise ValueError("Run Log must begin with user_message")
        if kind == "user_message" and self.has_user_message:
            raise ValueError("Run Log may contain only one user_message")
        if kind == "assistant_tool_call":
            if self.pending_call_id:
                raise ValueError("Run Log already has a pending tool call")
        elif kind == "tool_started":
            call_id = str(payload["tool_call_id"])
            if call_id != self.pending_call_id:
                raise ValueError("tool_started must match the pending tool call")
            if str(payload["tool_name"]) != self.pending_tool_name:
                raise ValueError("tool_started tool name does not match the pending call")
            if self.started_call_id:
                raise ValueError("pending tool call already started")
        elif kind == "tool_result":
            call_id = str(payload["tool_call_id"])
            outcome = ToolOutcome.from_dict(payload["outcome"])
            if call_id != self.pending_call_id:
                raise ValueError("tool_result must match the pending tool call")
            if str(payload["tool_name"]) != self.pending_tool_name:
                raise ValueError("tool_result tool name does not match the pending call")
            if outcome.execution_state == "not_started" and self.started_call_id:
                raise ValueError("started tool cannot finish as not_started")
            if outcome.execution_state != "not_started" and not self.started_call_id:
                raise ValueError("executed tool_result requires tool_started")
        elif self.pending_call_id and kind not in {"tool_started", "tool_result"}:
            raise ValueError("pending tool call must receive a result first")

    def apply(self, entry):
        self.check(entry.kind, entry.payload)
        if entry.kind == "user_message":
            self.has_user_message = True
        elif entry.kind == "assistant_tool_call":
            self.pending_call_id = entry.call_id
            self.pending_tool_name = entry.name
            self.started_call_id = ""
        elif entry.kind == "tool_started":
            self.started_call_id = entry.call_id
        elif entry.kind == "tool_result":
            self.pending_call_id = ""
            self.pending_tool_name = ""
            self.started_call_id = ""
        elif entry.kind in {"assistant_final", "run_stopped"}:
            self.terminal = True


def validate_run_events(events):
    protocol = _RunProtocol()
    for entry in events:
        protocol.apply(entry)
    return protocol


def _clip(text, limit=320):
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 2].rstrip() + " …"


def _summary_args(args, limit=360):
    """Keep operation identity without copying large mutation bodies."""
    bounded = {}
    for key, value in sorted(dict(args or {}).items()):
        if key in {"content", "old_text", "new_text"}:
            bounded[key] = f"<{len(str(value))} chars>"
        else:
            bounded[key] = value
    return _clip(
        json.dumps(bounded, ensure_ascii=False, sort_keys=True),
        limit,
    )


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
            "schema_version": RUN_LOG_SCHEMA_VERSION,
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
            "schema_version",
            "event_id",
            "sequence",
            "run_id",
            "task_id",
            "session_id",
            "kind",
            "timestamp",
            "payload",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("schema_version") != RUN_LOG_SCHEMA_VERSION
        ):
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
        if self.kind in {"user_message", "model_instruction", "assistant_final"}:
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
            outcome = dict(self.payload.get("outcome", {}) or {})
            return str(outcome.get("tool_name", self.payload.get("tool_name", "")))
        return ""

    @property
    def args(self):
        return dict(self.payload.get("args", {}) or {})

    @property
    def call_id(self):
        if self.kind == "assistant_tool_call":
            return str(self.payload.get("call_id", ""))
        outcome = dict(self.payload.get("outcome", {}) or {})
        return str(
            self.payload.get("tool_call_id", "")
            or outcome.get("tool_call_id", "")
        )

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
        artifact = dict(outcome.get("artifact", {}) or {})
        return str(artifact.get("artifact_id", ""))

    @property
    def covered_event_ids(self):
        return tuple(str(item) for item in self.payload.get("covered_event_ids", []))


@dataclass(frozen=True)
class RunCursor:
    sequence: int = 0
    event_id: str = ""

    def to_dict(self):
        return {"sequence": self.sequence, "event_id": self.event_id}


@dataclass
class RunReplayState:
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    working_state: WorkingState = field(default_factory=WorkingState)
    status: str = "running"
    stop_reason: str = ""
    final_answer: str = ""
    run_duration_ms: int = 0
    model_request_count: int = 0
    executed_tool_count: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    verification_counts: dict[str, int] = field(default_factory=dict)
    pending_operations: set[str] = field(default_factory=set)
    last_cursor: RunCursor = field(default_factory=RunCursor)

    def apply(self, entry):
        kind = entry.kind
        payload = dict(entry.payload)
        apply_task_event(self, entry)
        self.session_id = entry.session_id or self.session_id
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        self.last_cursor = RunCursor(entry.sequence, entry.event_id)
        if kind == "assistant_tool_call":
            call_id = entry.call_id
            if call_id:
                self.pending_operations.add(call_id)
        elif kind == "tool_result":
            outcome = dict(payload.get("outcome", {}) or {})
            call_id = str(payload.get("tool_call_id", "") or outcome.get("tool_call_id", ""))
            if call_id:
                self.pending_operations.discard(call_id)
            tool_name = str(outcome.get("tool_name", payload.get("tool_name", "")))
            status = str(outcome.get("status", "unknown"))
            if tool_name and outcome.get("execution_state") != "not_started":
                self.tool_counts[tool_name] = self.tool_counts.get(tool_name, 0) + 1
            self.outcome_counts[status] = self.outcome_counts.get(status, 0) + 1
        elif kind == "verification_result":
            status = str(payload.get("status", "unknown"))
            self.verification_counts[status] = self.verification_counts.get(status, 0) + 1
        elif kind in {"assistant_final", "run_stopped"}:
            self.run_duration_ms = int(payload.get("run_duration_ms", 0))
        return self

    @property
    def terminal(self):
        return self.status in {"completed", "stopped"}

    def task_state(self):
        return TaskState.from_dict(
            {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "working_state": self.working_state.to_dict(),
                "status": self.status,
                "executed_tool_count": self.executed_tool_count,
                "model_request_count": self.model_request_count,
                "stop_reason": self.stop_reason,
                "final_answer": self.final_answer,
            }
        ).to_dict()

    def summary(self):
        return {
            **self.task_state(),
            "session_id": self.session_id,
            "run_duration_ms": self.run_duration_ms,
            "kind_counts": dict(sorted(self.kind_counts.items())),
            "tool_counts": dict(sorted(self.tool_counts.items())),
            "outcome_counts": dict(sorted(self.outcome_counts.items())),
            "verification_counts": dict(sorted(self.verification_counts.items())),
            "pending_operations": sorted(self.pending_operations),
            "run_cursor": self.last_cursor.to_dict(),
        }


def replay_events(events):
    validate_run_events(events)
    projection = RunReplayState()
    for entry in events:
        projection.apply(entry)
    return projection


class RunLog:
    """Append-only Run facts plus the model-visible context projection."""

    def __init__(self, run_id, task_id, session_id, store, events=()):
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self.session_id = str(session_id)
        self.store = store
        self.generation = 1
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

    def append_user(self, content):
        return self.append("user_message", {"content": str(content)})

    def append_tool_call(self, call):
        return self.append(
            "assistant_tool_call",
            {"name": call.name, "args": dict(call.args), "call_id": call.call_id},
        )

    def append_tool_started(
        self,
        call,
        *,
        risky,
        effect_scope,
        potential_effects,
    ):
        return self.append(
            "tool_started",
            {
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                "risky": bool(risky),
                "effect_scope": str(effect_scope),
                "potential_effects": list(potential_effects),
            },
        )

    def append_tool_result(
        self,
        outcome,
        *,
        workspace_revision,
        recovered_from_interruption=False,
    ):
        payload = {
            "tool_call_id": outcome.tool_call_id,
            "tool_name": outcome.tool_name,
            "workspace_revision": int(workspace_revision),
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

    def append_final(self, content, *, run_duration_ms=0):
        self._require_no_pending()
        return self.append(
            "assistant_final",
            {
                "content": str(content),
                "stop_reason": STOP_REASON_FINAL_ANSWER_RETURNED,
                "run_duration_ms": int(run_duration_ms),
            },
        )

    def append_stopped(self, content, stop_reason, *, run_duration_ms=0):
        self._require_no_pending()
        return self.append(
            "run_stopped",
            {
                "content": str(content),
                "stop_reason": str(stop_reason),
                "run_duration_ms": int(run_duration_ms),
            },
        )

    def pending_call_id(self):
        return self._protocol.pending_call_id

    def _require_no_pending(self):
        if self.pending_call_id():
            raise RuntimeError("pending tool call must receive a result first")

    def reconcile_interrupted(self, runtime):
        pending = self.pending_call_id()
        if not pending:
            return ()
        call = next(
            entry
            for entry in reversed(self._events)
            if entry.kind == "assistant_tool_call" and entry.call_id == pending
        )
        started = next(
            (
                entry
                for entry in reversed(self._events)
                if entry.kind == "tool_started" and entry.call_id == pending
            ),
            None,
        )
        if started is None:
            detail = "tool call was persisted but never entered execution"
            outcome = ToolOutcome(
                tool_call_id=pending,
                tool_name=call.name,
                status="error",
                execution_state="not_started",
                side_effect_state="none",
                content=detail,
                correction_action="wait",
                failure=FailureInfo(
                    "operation_not_started",
                    detail,
                    "retry_after_wait",
                ),
            )
        else:
            potential = list(started.payload.get("potential_effects", []))
            changed = []
            for effect in potential:
                logical = str(effect.get("path", ""))
                if not logical:
                    continue
                path = Path(logical)
                if not path.is_absolute():
                    path = runtime.workspace.resolve_path(logical)
                before = str(effect.get("before_state", ""))
                after = runtime.workspace.path_state(path)
                if before != after:
                    changed.append(logical)
            effect_scope = str(started.payload.get("effect_scope", "none"))
            unknown = effect_scope in {"workspace", "mixed"} and not potential
            uncertain = bool(changed or unknown)
            detail = "tool execution was interrupted before a durable result"
            outcome = ToolOutcome(
                tool_call_id=pending,
                tool_name=call.name,
                status="partial_success" if uncertain else "error",
                execution_state="failed",
                side_effect_state="partial" if changed else ("unknown" if unknown else "none"),
                content=detail,
                correction_action="stop_route" if uncertain else "wait",
                failure=FailureInfo(
                    "operation_interrupted",
                    detail,
                    "no_retry" if uncertain else "retry_after_wait",
                ),
                affected_paths=tuple(changed),
                effect_scope=effect_scope if changed or unknown else "none",
            )
        entry = self.append_tool_result(
            outcome,
            workspace_revision=runtime.workspace.revision,
            recovered_from_interruption=True,
        )
        self.reconciled_outcomes.append((outcome, entry))
        return tuple(self.reconciled_outcomes)

    def context_events(self):
        calls = {
            entry.call_id
            for entry in self._events
            if entry.kind == "assistant_tool_call" and entry.call_id
        }
        return tuple(
            entry
            for entry in self._events
            if entry.kind in CONTEXT_KINDS
            and (entry.kind != "tool_result" or entry.call_id in calls)
        )

    def active_events(self):
        context = self.context_events()
        covered = {
            item
            for entry in context
            if entry.kind == "compaction"
            for item in entry.covered_event_ids
        }
        return tuple(entry for entry in context if entry.event_id not in covered)

    def compact(self, *, retain_tokens, token_counter, summary_builder=None):
        active = list(self.active_events())
        units = []
        index = 0
        while index < len(active):
            entry = active[index]
            if entry.kind == "assistant_tool_call":
                if index + 1 >= len(active):
                    return None
                result = active[index + 1]
                if result.kind != "tool_result" or result.call_id != entry.call_id:
                    raise RuntimeError("Run Log tool batch is not contiguous")
                units.append((entry, result))
                index += 2
                continue
            if entry.kind == "tool_result":
                raise RuntimeError("Run Log contains an orphan tool result")
            units.append((entry,))
            index += 1
        retained_tokens = 0
        retained_units = 0
        limit = max(1, int(retain_tokens))
        for unit in reversed(units):
            text = "\n".join(self._render_event(item) for item in unit)
            unit_tokens = max(1, int(token_counter(text)))
            if retained_units and retained_tokens + unit_tokens > limit:
                break
            retained_tokens += unit_tokens
            retained_units += 1
        cut = max(0, len(units) - retained_units)
        compacted = tuple(item for unit in units[:cut] for item in unit)
        if not compacted:
            return None
        summary = (
            summary_builder(compacted)
            if summary_builder is not None
            else self._summary_text(compacted)
        )
        source = "\n".join(self._render_event(entry) for entry in compacted)
        if token_counter(summary) >= token_counter(source):
            return None
        event = self._commit_compaction(
            summary,
            [entry.event_id for entry in compacted],
        )
        return event, {
            "mode": "runtime_summary",
            "covered_events": len(compacted),
            "retained_events": sum(len(unit) for unit in units[cut:]),
            "retained_tokens": retained_tokens,
            "summary_tokens": token_counter(summary),
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

    def _summary_text(self, events):
        lines = ["Earlier run summary:"]
        index = 0
        while index < len(events):
            entry = events[index]
            if entry.kind == "model_instruction":
                lines.append(f"- Instruction: {_clip(entry.content)}")
                index += 1
                continue
            if entry.kind == "compaction":
                lines.extend(
                    line
                    for line in entry.content.splitlines()
                    if line.startswith("- ")
                )
                index += 1
                continue
            if entry.kind != "assistant_tool_call":
                index += 1
                continue
            if index + 1 >= len(events):
                raise RuntimeError("Run Log compaction received an incomplete tool batch")
            result = events[index + 1]
            if result.kind != "tool_result" or result.call_id != entry.call_id:
                raise RuntimeError("Run Log compaction received a mismatched tool batch")
            if not (
                entry.name == "update_working_state"
                and result.outcome_status == "success"
            ):
                lines.append(
                    f"- Tool transaction: {entry.name} {_summary_args(entry.args)} "
                    f"-> {result.outcome_status}; result: {_clip(result.content, 240)}"
                )
            index += 2
        return "\n".join(lines)

    @staticmethod
    def _render_event(entry):
        if entry.kind == "assistant_tool_call":
            return (
                f"[assistant/tool] {entry.name} "
                + json.dumps(entry.args or {}, ensure_ascii=False, sort_keys=True)
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
    def _without_projected_working_updates(events):
        selected = []
        index = 0
        while index < len(events):
            entry = events[index]
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

    def render_projection(self, query, exclude_user_content=None):
        del query
        active = self.active_events()
        selected = self._without_projected_working_updates(active)
        if exclude_user_content is not None:
            for index in range(len(selected) - 1, -1, -1):
                entry = selected[index]
                if entry.kind == "user_message" and entry.content == str(
                    exclude_user_content
                ):
                    selected.pop(index)
                    break
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
