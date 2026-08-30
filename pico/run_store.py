"""Single-writer Run Log and artifact directory storage."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import ArtifactStore
from .run_log import RunEvent, replay_events, validate_run_events
from .run_projection import RunCursor

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _run_id(value):
    run_id = str(value.run_id) if hasattr(value, "run_id") else str(value)
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run id")
    return run_id


class RunStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._cursors: dict[str, RunCursor] = {}

    def run_dir(self, run_id):
        directory = self.root / _run_id(run_id)
        if directory.is_symlink():
            raise ValueError("run directory must not be a symlink")
        return directory

    def events_path(self, run_id):
        path = self.run_dir(run_id) / "events.jsonl"
        if path.is_symlink():
            raise ValueError("Run Log must not be a symlink")
        return path

    def artifact_dir(self, run_id):
        path = self.run_dir(run_id) / "artifacts"
        if path.is_symlink():
            raise ValueError("artifact directory must not be a symlink")
        return path

    def has_events(self, run_id):
        return self.events_path(run_id).is_file()

    @staticmethod
    def _repair_incomplete_tail(path):
        data = path.read_bytes()
        if not data or data.endswith(b"\n"):
            return data
        last_newline = data.rfind(b"\n")
        repaired = data[: last_newline + 1] if last_newline >= 0 else b""
        with path.open("r+b") as handle:
            handle.truncate(len(repaired))
            handle.flush()
            os.fsync(handle.fileno())
        return repaired

    def read_events(self, run_id):
        run_id = _run_id(run_id)
        path = self.events_path(run_id)
        if not path.exists():
            return []
        data = self._repair_incomplete_tail(path)
        events = []
        for number, raw in enumerate(data.splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Run Log line {number} is not valid JSON"
                ) from exc
            entry = RunEvent.from_dict(value)
            expected = len(events) + 1
            if entry.run_id != run_id:
                raise ValueError("Run event belongs to another run")
            if entry.sequence != expected:
                raise ValueError("Run Log sequence is not contiguous")
            if entry.event_id != f"{run_id}:event:{expected:06d}":
                raise ValueError("Run event id does not match its sequence")
            if events:
                first = events[0]
                if (
                    entry.task_id != first.task_id
                    or entry.session_id != first.session_id
                ):
                    raise ValueError("Run Log identity changed within one run")
            events.append(entry)
        self._cursors[run_id] = (
            RunCursor(events[-1].sequence, events[-1].event_id)
            if events
            else RunCursor()
        )
        validate_run_events(events)
        return events

    def cursor(self, run_id):
        run_id = _run_id(run_id)
        if run_id not in self._cursors:
            self.read_events(run_id)
        return self._cursors.get(run_id, RunCursor())

    def append_event(
        self,
        run_id,
        task_id,
        session_id,
        kind,
        payload=None,
        *,
        protocol_checked=False,
    ):
        run_id = _run_id(run_id)
        payload = dict(payload or {})
        if not protocol_checked:
            protocol = validate_run_events(self.read_events(run_id))
            protocol.check(str(kind), payload)
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        cursor = self.cursor(run_id)
        sequence = cursor.sequence + 1
        entry = RunEvent(
            event_id=f"{run_id}:event:{sequence:06d}",
            sequence=sequence,
            run_id=run_id,
            task_id=str(task_id),
            session_id=str(session_id),
            kind=str(kind),
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        encoded = (
            json.dumps(entry.to_dict(), sort_keys=True, ensure_ascii=True) + "\n"
        ).encode("utf-8")
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._cursors[run_id] = RunCursor(sequence, entry.event_id)
        return entry

    def replay(self, run_id):
        run_id = _run_id(run_id)
        projection = replay_events(self.read_events(run_id))
        final_diff = projection.final_diff
        if final_diff is not None and final_diff.diff_artifact_id:
            descriptor, _data = ArtifactStore(self, lambda text: text).read_internal(
                run_id,
                final_diff.diff_artifact_id,
                expected_kind="final_workspace_diff",
            )
            if int(descriptor["size_bytes"]) != final_diff.diff_bytes:
                raise ValueError("terminal final Diff descriptor size mismatch")
        return projection

    def find_active_run(self, session_id):
        if not self.root.exists():
            return "", (), None
        candidates = []
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                events = self.read_events(directory.name)
            except (OSError, ValueError):
                continue
            if not events or events[0].session_id != str(session_id):
                continue
            projection = replay_events(events)
            if not projection.terminal:
                candidates.append(
                    (
                        events[-1].timestamp,
                        directory.name,
                        tuple(events),
                        projection,
                    )
                )
        if candidates:
            _timestamp, run_id, events, projection = max(
                candidates,
                key=lambda item: (item[0], item[1]),
            )
            return run_id, events, projection
        return "", (), None
