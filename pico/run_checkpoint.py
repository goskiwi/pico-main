"""Persist one stable Run Projection, Context State, and Event Log cursor."""

import json
from pathlib import Path

from .compaction_summary import CompactedContext
from .history import CONTEXT_KINDS, ContextState, RunHistory
from .persistence import atomic_write_json
from .run_log import RunEvent
from .run_projection import RunProjection


def write_run_checkpoint(path, run_log, event_log_offset):
    projection = run_log.projection
    checkpoint = {
        "run_id": projection.run_id,
        "session_id": projection.session_id,
        "last_sequence": projection.last_sequence,
        "event_log_offset": int(event_log_offset),
        "run_state": projection.checkpoint_state(),
        "context_state": run_log.context_state.to_dict(),
    }
    atomic_write_json(path, checkpoint)
    return checkpoint


def _read_context_state(value, projection):
    if not isinstance(value, dict) or set(value) != {
        "compacted",
        "recent_events",
    }:
        raise ValueError("invalid Run checkpoint Context State")
    if not isinstance(value["recent_events"], list):
        raise TypeError("Run checkpoint recent_events must be a list")
    compacted = (
        CompactedContext.from_dict(value["compacted"])
        if value["compacted"] is not None
        else None
    )
    recent = [RunEvent.from_dict(item) for item in value["recent_events"]]
    if len({event.event_id for event in recent}) != len(recent):
        raise ValueError("Run checkpoint Context contains duplicate events")
    previous_sequence = (
        compacted.covered_through_sequence if compacted is not None else 0
    )
    if previous_sequence > projection.last_sequence:
        raise ValueError("Run checkpoint Context coverage is inconsistent")
    for event in recent:
        if (
            event.run_id != projection.run_id
            or event.session_id != projection.session_id
            or event.sequence <= previous_sequence
            or event.sequence > projection.last_sequence
            or event.kind not in CONTEXT_KINDS - {"compaction"}
            or event.event_id
            != f"{event.run_id}:event:{event.sequence:06d}"
        ):
            raise ValueError("Run checkpoint Context event is inconsistent")
        previous_sequence = event.sequence
    try:
        RunHistory._history_units(recent)
    except RuntimeError as exc:
        raise ValueError("Run checkpoint Context contains an incomplete turn") from exc
    return ContextState(compacted=compacted, recent_events=recent)


def read_run_checkpoint(path, *, expected_run_id):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Run checkpoint must not be a symlink")
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "run_id",
        "session_id",
        "last_sequence",
        "event_log_offset",
        "run_state",
        "context_state",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid Run checkpoint fields")
    if str(value["run_id"]) != str(expected_run_id):
        raise ValueError("Run checkpoint belongs to another Run")
    projection = RunProjection.from_checkpoint_state(
        value["run_state"],
        run_id=value["run_id"],
        session_id=value["session_id"],
        last_sequence=value["last_sequence"],
    )
    offset = int(value["event_log_offset"])
    if offset < 0:
        raise ValueError("Run checkpoint has an invalid Event offset")
    context_state = _read_context_state(value["context_state"], projection)
    return projection, context_state, offset
