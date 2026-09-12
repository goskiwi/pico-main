"""Single-writer Run Log and artifact storage within one Session's runs directory."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .artifacts import ArtifactStore
from .run_checkpoint import read_run_checkpoint, write_run_checkpoint
from .run_log import RunEvent, RunLog, replay_events

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CHECKPOINT_EVENT_INTERVAL = 10_000
CHECKPOINT_BYTE_INTERVAL = 2 * 1024 * 1024


def _run_id(value):
    run_id = str(value.run_id) if hasattr(value, "run_id") else str(value)
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run id")
    return run_id


class RunStore:
    def __init__(
        self,
        root,
        *,
        trace=None,
        checkpoint_event_interval=CHECKPOINT_EVENT_INTERVAL,
        checkpoint_byte_interval=CHECKPOINT_BYTE_INTERVAL,
    ):
        self.root = Path(root).resolve()
        self.trace = trace
        self.root.mkdir(parents=True, exist_ok=True)
        self._sequences: dict[str, int] = {}
        self._checkpoint_cursors: dict[str, tuple[int, int]] = {}
        self.checkpoint_event_interval = int(checkpoint_event_interval)
        self.checkpoint_byte_interval = int(checkpoint_byte_interval)
        if self.checkpoint_event_interval < 1 or self.checkpoint_byte_interval < 1:
            raise ValueError("checkpoint intervals must be positive")

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

    def checkpoint_path(self, run_id):
        path = self.run_dir(run_id) / "checkpoint.json"
        if path.is_symlink():
            raise ValueError("Run checkpoint must not be a symlink")
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

    def _read_tail(self, run_id, *, offset):
        path = self.events_path(run_id)
        size = path.stat().st_size
        if offset < 0 or offset > size:
            raise ValueError("Run checkpoint Event offset is outside the log")
        events = []
        torn_at = None
        with path.open("rb") as handle:
            if offset:
                handle.seek(offset - 1)
                if handle.read(1) != b"\n":
                    raise ValueError("Run checkpoint Event offset is not a line boundary")
            handle.seek(offset)
            number = 0
            while True:
                start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                number += 1
                if not raw.endswith(b"\n"):
                    torn_at = start
                    break
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Run Log tail line {number} is not valid JSON"
                    ) from exc
                events.append(RunEvent.from_dict(value))
        if torn_at is not None:
            with path.open("r+b") as handle:
                handle.truncate(torn_at)
                handle.flush()
                os.fsync(handle.fileno())
        return events

    def _remember_cursor(self, run_id, events):
        self._sequences[_run_id(run_id)] = events[-1].sequence if events else 0

    def last_sequence(self, run_id):
        run_id = _run_id(run_id)
        if run_id not in self._sequences:
            self.read_events(run_id)
        return self._sequences.get(run_id, 0)

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
            offset = handle.tell()
        self._sequences[entry.run_id] = entry.sequence
        return offset

    def maybe_checkpoint(self, run_log, entry, event_log_offset):
        projection = run_log.projection
        if projection.terminal or run_log.pending_tool_call() is not None:
            return False
        previous_sequence, previous_offset = self._checkpoint_cursors.get(
            run_log.run_id, (0, 0)
        )
        due = bool(
            entry.kind == "compaction"
            or projection.last_sequence - previous_sequence
            >= self.checkpoint_event_interval
            or event_log_offset - previous_offset >= self.checkpoint_byte_interval
        )
        if not due:
            return False
        try:
            write_run_checkpoint(
                self.checkpoint_path(run_log.run_id), run_log, event_log_offset
            )
        except (OSError, TypeError, ValueError):
            return False
        self._checkpoint_cursors[run_log.run_id] = (
            projection.last_sequence,
            event_log_offset,
        )
        run_log._events.clear()
        return True

    def load_run(self, run_id):
        """Load one ready RunLog whose Projection comes from the same snapshot."""

        run_id = _run_id(run_id)
        checkpoint = self.checkpoint_path(run_id)
        if checkpoint.is_file():
            try:
                projection, history, guidance, offset = read_run_checkpoint(
                    checkpoint, expected_run_id=run_id
                )
                checkpoint_sequence = projection.last_sequence
                tail = self._read_tail(run_id, offset=offset)
                log = RunLog._from_checkpoint(
                    projection=projection,
                    history_events=history,
                    latest_user_guidance_event=guidance,
                    tail_events=tail,
                    store=self,
                )
                self._sequences[run_id] = log.projection.last_sequence
                self._checkpoint_cursors[run_id] = (
                    checkpoint_sequence,
                    offset,
                )
            except (OSError, TypeError, ValueError):
                log = self._load_full_and_rebuild_checkpoint(run_id)
        else:
            log = self._load_full_and_rebuild_checkpoint(run_id)
        final_diff = log.projection.final_diff
        if final_diff is not None and final_diff.artifact_id:
            descriptor, _data = ArtifactStore(self, lambda text: text).read_internal(
                run_id,
                final_diff.artifact_id,
                expected_kind="final_workspace_diff",
            )
            if int(descriptor["size_bytes"]) != final_diff.size_bytes:
                raise ValueError("terminal final Diff descriptor size mismatch")
        return log

    def _load_full_and_rebuild_checkpoint(self, run_id):
        events = self._read_events(run_id)
        log = RunLog._from_events(events, self, expected_run_id=run_id)
        self._remember_cursor(run_id, events)
        if not log.projection.terminal and log.pending_tool_call() is None:
            offset = self.events_path(run_id).stat().st_size
            try:
                write_run_checkpoint(self.checkpoint_path(run_id), log, offset)
            except (OSError, TypeError, ValueError):
                return log
            else:
                self._checkpoint_cursors[run_id] = (
                    log.projection.last_sequence,
                    offset,
                )
                log._events.clear()
        return log

    def replay(self, run_id):
        return self.load_run(run_id).projection

    def find_active_run(self, session_id):
        if not self.root.exists():
            return None
        candidates = []
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                log = self.load_run(directory.name)
            except (OSError, ValueError):
                continue
            if log.session_id != str(session_id):
                continue
            if not log.projection.terminal:
                candidates.append(
                    (
                        log.projection.last_timestamp,
                        directory.name,
                        log,
                    )
                )
        if candidates:
            _timestamp, _run_id, log = max(
                candidates,
                key=lambda item: (item[0], item[1]),
            )
            return log
        return None
