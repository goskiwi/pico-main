"""Rebuildable Projection and effective-History checkpoint persistence."""

import json
from pathlib import Path

from .history import CONTEXT_KINDS, RunHistory
from .persistence import atomic_write_json
from .run_log import RunEvent
from .run_projection import RunProjection


def write_run_checkpoint(path, run_log, event_log_offset):
    projection = run_log.projection
    payload = {
        "run_id": projection.run_id,
        "session_id": projection.session_id,
        "last_sequence": projection.last_sequence,
        "event_log_offset": int(event_log_offset),
        "projection": projection.to_checkpoint(),
        "history": {
            "events": [
                event.to_dict() for event in run_log.effective_history_events
            ],
            "latest_user_guidance": (
                run_log.latest_user_guidance_event.to_dict()
                if run_log.latest_user_guidance_event
                else None
            ),
        },
    }
    atomic_write_json(path, payload)
    return payload


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
        "projection",
        "history",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid Run checkpoint fields")
    if str(value["run_id"]) != str(expected_run_id):
        raise ValueError("Run checkpoint belongs to another Run")
    projection = RunProjection.from_checkpoint(value["projection"])
    if (
        projection.run_id != str(value["run_id"])
        or projection.session_id != str(value["session_id"])
        or projection.last_sequence != int(value["last_sequence"])
    ):
        raise ValueError("Run checkpoint Projection identity is inconsistent")
    if projection.pending_tool.call is not None:
        raise ValueError("Run checkpoint contains a pending tool")
    offset = int(value["event_log_offset"])
    if offset < 0:
        raise ValueError("Run checkpoint has an invalid Event offset")
    history = value["history"]
    if not isinstance(history, dict) or set(history) != {
        "events",
        "latest_user_guidance",
    }:
        raise ValueError("invalid Run checkpoint History")
    if not isinstance(history["events"], list):
        raise TypeError("Run checkpoint History events must be a list")
    events = tuple(RunEvent.from_dict(item) for item in history["events"])
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("Run checkpoint History contains duplicate events")
    for event in events:
        if (
            event.run_id != projection.run_id
            or event.session_id != projection.session_id
            or event.sequence > projection.last_sequence
            or event.kind not in CONTEXT_KINDS
            or event.event_id
            != f"{event.run_id}:event:{event.sequence:06d}"
        ):
            raise ValueError("Run checkpoint History event is inconsistent")
    RunHistory._history_units(events)
    latest_raw = history["latest_user_guidance"]
    latest = RunEvent.from_dict(latest_raw) if latest_raw is not None else None
    if latest is not None and (
        latest.kind != "user_guidance"
        or latest.run_id != projection.run_id
        or latest.session_id != projection.session_id
        or latest.sequence > projection.last_sequence
        or latest.event_id
        != f"{latest.run_id}:event:{latest.sequence:06d}"
    ):
        raise ValueError("Run checkpoint latest guidance is inconsistent")
    return projection, events, latest, offset
