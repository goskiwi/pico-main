"""Single-writer Run Log and artifact directory storage."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .artifacts import ArtifactStore
from .run_log import RunEvent, RunLog, replay_events
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

    def _read_events(self, run_id):
        """Read records and repair only a torn final line; no sequence policy here."""
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
            events.append(RunEvent.from_dict(value))
        return events

    def read_events(self, run_id):
        events = self._read_events(run_id)
        replay_events(events, expected_run_id=_run_id(run_id))
        self._remember_cursor(run_id, events)
        return events

    def _remember_cursor(self, run_id, events):
        self._cursors[_run_id(run_id)] = (
            RunCursor(events[-1].sequence, events[-1].event_id)
            if events
            else RunCursor()
        )

    def cursor(self, run_id):
        run_id = _run_id(run_id)
        if run_id not in self._cursors:
            self.read_events(run_id)
        return self._cursors.get(run_id, RunCursor())

    def _append_event(self, entry):
        """Persist a complete event; only RunLog authorizes new events."""
        path = self.events_path(entry.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(entry.to_dict(), sort_keys=True, ensure_ascii=True) + "\n"
        ).encode("utf-8")
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._cursors[entry.run_id] = RunCursor(entry.sequence, entry.event_id)

    def load_run(self, run_id):
        """Load a ready RunLog and projection from the same validated snapshot."""

        run_id = _run_id(run_id)
        events = self._read_events(run_id)
        log, projection = RunLog._from_events(events, self, expected_run_id=run_id)
        self._remember_cursor(run_id, log.events)
        final_diff = projection.final_diff
        if final_diff is not None and final_diff.artifact_id:
            descriptor, _data = ArtifactStore(self, lambda text: text).read_internal(
                run_id,
                final_diff.artifact_id,
                expected_kind="final_workspace_diff",
            )
            if int(descriptor["size_bytes"]) != final_diff.size_bytes:
                raise ValueError("terminal final Diff descriptor size mismatch")
        return log, projection

    def replay(self, run_id):
        _log, projection = self.load_run(run_id)
        return projection

    def find_active_run(self, session_id):
        if not self.root.exists():
            return None, None
        candidates = []
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                log, projection = self.load_run(directory.name)
            except (OSError, ValueError):
                continue
            if log.session_id != str(session_id):
                continue
            if not projection.terminal:
                candidates.append(
                    (
                        log.events[-1].timestamp,
                        directory.name,
                        log,
                        projection,
                    )
                )
        if candidates:
            _timestamp, _run_id, log, projection = max(
                candidates,
                key=lambda item: (item[0], item[1]),
            )
            return log, projection
        return None, None
