"""Versioned, hash-chained Runtime events and deterministic projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

EVENT_SCHEMA_VERSION = "runtime-event-v1"
EVENT_TYPES = frozenset(
    {
        "checkpoint_created",
        "completion_blocked",
        "context_folded",
        "memory_selection",
        "model_parsed",
        "model_requested",
        "operation_finished",
        "operation_started",
        "progress_decided",
        "prompt_built",
        "run_finished",
        "run_resumed",
        "run_started",
        "runtime_identity_mismatch",
        "tool_rejected",
        "verification_finished",
        "verification_started",
    }
)
EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "sequence",
        "run_id",
        "task_id",
        "event_type",
        "causation_id",
        "correlation_id",
        "workspace_fingerprint",
        "payload",
        "previous_hash",
        "event_hash",
        "created_at",
    }
)
GENESIS_HASH = "0" * 64


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def event_digest(value):
    unsigned = {key: item for key, item in dict(value).items() if key != "event_hash"}
    return hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventCursor:
    sequence: int = 0
    event_id: str = ""
    event_hash: str = GENESIS_HASH

    def to_dict(self):
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_hash": self.event_hash,
        }


def validate_event(value, previous=None):
    if not isinstance(value, dict) or set(value) != EVENT_FIELDS:
        raise ValueError("invalid Runtime event fields")
    if value["schema_version"] != EVENT_SCHEMA_VERSION:
        raise ValueError("unsupported Runtime event schema")
    if value["event_type"] not in EVENT_TYPES:
        raise ValueError("unsupported Runtime event type")
    if not isinstance(value["payload"], dict):
        raise TypeError("Runtime event payload must be an object")
    sequence = int(value["sequence"])
    expected_sequence = 1 if previous is None else int(previous["sequence"]) + 1
    if sequence != expected_sequence:
        raise ValueError("Runtime event sequence is not contiguous")
    if value["event_id"] != f"{value['run_id']}:evt:{sequence:06d}":
        raise ValueError("Runtime event id does not match its sequence")
    expected_previous_hash = GENESIS_HASH if previous is None else previous["event_hash"]
    if value["previous_hash"] != expected_previous_hash:
        raise ValueError("Runtime event hash chain is broken")
    if value["event_hash"] != event_digest(value):
        raise ValueError("Runtime event digest mismatch")
    return value


def cursor_for(events):
    events = list(events)
    if not events:
        return EventCursor()
    last = events[-1]
    return EventCursor(int(last["sequence"]), str(last["event_id"]), str(last["event_hash"]))


@dataclass
class RunProjection:
    """Replayable run state used by resume checks and CLI analysis."""

    run_id: str = ""
    task_id: str = ""
    user_request: str = ""
    status: str = "running"
    stop_reason: str = ""
    final_answer: str = ""
    attempts: int = 0
    tool_steps: int = 0
    last_tool: str = ""
    checkpoint_id: str = ""
    event_counts: dict[str, int] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    progress_counts: dict[str, int] = field(default_factory=dict)
    verification_counts: dict[str, int] = field(default_factory=dict)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_cursor: EventCursor = field(default_factory=EventCursor)

    def apply(self, event):
        event_type = str(event["event_type"])
        payload = dict(event["payload"])
        self.run_id = str(event["run_id"])
        self.task_id = str(event["task_id"] or self.task_id)
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
        self.last_cursor = EventCursor(
            int(event["sequence"]), str(event["event_id"]), str(event["event_hash"])
        )
        if event_type == "run_started":
            self.user_request = str(payload.get("user_request", self.user_request))
        elif event_type == "model_requested":
            self.attempts = max(self.attempts, int(payload.get("attempts", self.attempts)))
        elif event_type == "operation_started":
            call_id = str(event["correlation_id"] or payload.get("tool_call_id", ""))
            if call_id:
                self.operations[call_id] = {"state": "started", **payload}
        elif event_type == "operation_finished":
            call_id = str(event["correlation_id"] or payload.get("tool_call_id", ""))
            if call_id:
                self.operations[call_id] = {"state": "finished", **payload}
            outcome = dict(payload.get("outcome", {}) or {})
            tool_name = str(outcome.get("tool_name", payload.get("tool_name", "")))
            status = str(outcome.get("status", "unknown"))
            if tool_name:
                self.tool_steps += 1
                self.last_tool = tool_name
                self.tool_counts[tool_name] = self.tool_counts.get(tool_name, 0) + 1
            self.outcome_counts[status] = self.outcome_counts.get(status, 0) + 1
        elif event_type == "tool_rejected":
            outcome = dict(payload.get("outcome", {}) or {})
            status = str(outcome.get("status", "rejected"))
            self.outcome_counts[status] = self.outcome_counts.get(status, 0) + 1
        elif event_type == "verification_finished":
            status = str(payload.get("status", "unknown"))
            self.verification_counts[status] = self.verification_counts.get(status, 0) + 1
        elif event_type == "progress_decided":
            decision = str(payload.get("decision", "unknown"))
            self.progress_counts[decision] = self.progress_counts.get(decision, 0) + 1
        elif event_type == "checkpoint_created":
            self.checkpoint_id = str(payload.get("checkpoint_id", self.checkpoint_id))
        elif event_type == "run_finished":
            self.status = str(payload.get("status", self.status))
            self.stop_reason = str(payload.get("stop_reason", self.stop_reason))
            self.final_answer = str(payload.get("final_answer", self.final_answer))
        return self

    def operation_receipt(self, call_id):
        return self.operations.get(str(call_id))

    def task_state(self, snapshot=None):
        """Rebuild mutable task counters from events; snapshot supplies checkpoint-only fields."""
        snapshot = dict(snapshot or {})
        return {
            "run_id": self.run_id or str(snapshot.get("run_id", "")),
            "task_id": self.task_id or str(snapshot.get("task_id", "")),
            "user_request": self.user_request or str(snapshot.get("user_request", "")),
            "status": self.status if self.stop_reason else str(snapshot.get("status", "running")),
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "checkpoint_id": self.checkpoint_id or str(snapshot.get("checkpoint_id", "")),
            "resume_status": str(snapshot.get("resume_status", "")),
        }

    def summary(self):
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "attempts": self.attempts,
            "tool_steps": self.tool_steps,
            "last_tool": self.last_tool,
            "checkpoint_id": self.checkpoint_id,
            "event_counts": dict(sorted(self.event_counts.items())),
            "tool_counts": dict(sorted(self.tool_counts.items())),
            "outcome_counts": dict(sorted(self.outcome_counts.items())),
            "progress_counts": dict(sorted(self.progress_counts.items())),
            "verification_counts": dict(sorted(self.verification_counts.items())),
            "pending_operations": sorted(
                call_id for call_id, item in self.operations.items() if item.get("state") != "finished"
            ),
            "event_cursor": self.last_cursor.to_dict(),
        }


def replay_events(events: Iterable[dict[str, Any]]):
    projection = RunProjection()
    previous = None
    for event in events:
        validate_event(event, previous)
        projection.apply(event)
        previous = event
    return projection
