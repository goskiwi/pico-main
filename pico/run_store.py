"""运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、Runtime events 和 report。两者分开后，恢复现场和复盘证据不会混在一起。
"""

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .events import (
    EVENT_SCHEMA_VERSION,
    EventCursor,
    event_digest,
    replay_events,
    validate_event,
)


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._event_states = {}

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def events_path(self, run_id):
        return self.run_dir(run_id) / "events.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def context_path(self, run_id):
        return self.run_dir(run_id) / "context.jsonl"

    def artifact_dir(self, run_id):
        return self.run_dir(run_id) / "artifacts"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, task_state.to_dict())
        return path

    def read_events(self, run_id):
        path = self.events_path(run_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            text = handle.read()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return self._decode_events(run_id, text)

    @staticmethod
    def _decode_events(run_id, text):
        events = []
        previous = None
        for line in text.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("run_id") != _run_id(run_id):
                raise ValueError("Runtime event belongs to another run")
            validate_event(event, previous)
            events.append(event)
            previous = event
        return events

    def event_cursor(self, run_id):
        run_id = _run_id(run_id)
        if run_id in self._event_states:
            return self._event_states[run_id]["cursor"]
        events = self.read_events(run_id)
        if not events:
            cursor = EventCursor()
        else:
            last = events[-1]
            cursor = EventCursor(last["sequence"], last["event_id"], last["event_hash"])
        path = self.events_path(run_id)
        self._event_states[run_id] = {
            "cursor": cursor,
            "offset": path.stat().st_size if path.exists() else 0,
            "last_event": events[-1] if events else None,
        }
        return cursor

    @staticmethod
    def _decode_event_tail(run_id, data, previous):
        if not data:
            return [], previous
        if not data.endswith(b"\n"):
            raise ValueError("Runtime event log has a truncated tail")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Runtime event log tail is not UTF-8") from exc
        events = []
        for line in text.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("run_id") != _run_id(run_id):
                raise ValueError("Runtime event belongs to another run")
            validate_event(event, previous)
            events.append(event)
            previous = event
        return events, previous

    def append_event(
        self,
        run_id,
        task_id,
        event_type,
        payload=None,
        *,
        correlation_id="",
        workspace_fingerprint="",
        causation_id=None,
    ):
        run_id = _run_id(run_id)
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            size = os.fstat(handle.fileno()).st_size
            state = self._event_states.get(run_id)
            if state is None:
                handle.seek(0)
                data = handle.read()
                if data and not data.endswith(b"\n"):
                    raise ValueError("Runtime event log has a truncated tail")
                events = self._decode_events(run_id, data.decode("utf-8"))
                last_event = events[-1] if events else None
                cursor = (
                    EventCursor(
                        last_event["sequence"],
                        last_event["event_id"],
                        last_event["event_hash"],
                    )
                    if last_event
                    else EventCursor()
                )
                state = {"cursor": cursor, "offset": size, "last_event": last_event}
            else:
                expected_offset = int(state["offset"])
                if size < expected_offset:
                    raise ValueError("Runtime event log was truncated after opening")
                if size > expected_offset:
                    handle.seek(expected_offset)
                    appended, last_event = self._decode_event_tail(
                        run_id,
                        handle.read(size - expected_offset),
                        state["last_event"],
                    )
                    if appended:
                        last = appended[-1]
                        state["cursor"] = EventCursor(
                            last["sequence"], last["event_id"], last["event_hash"]
                        )
                        state["last_event"] = last_event
                    state["offset"] = size
                cursor = state["cursor"]
            sequence = cursor.sequence + 1
            event = {
                "schema_version": EVENT_SCHEMA_VERSION,
                "event_id": f"{run_id}:evt:{sequence:06d}",
                "sequence": sequence,
                "run_id": run_id,
                "task_id": str(task_id),
                "event_type": str(event_type),
                "causation_id": cursor.event_id if causation_id is None else str(causation_id),
                "correlation_id": str(correlation_id),
                "workspace_fingerprint": str(workspace_fingerprint),
                "payload": dict(payload or {}),
                "previous_hash": cursor.event_hash,
                "event_hash": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            event["event_hash"] = event_digest(event)
            validate_event(event, state["last_event"])
            encoded = (json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
            handle.seek(0, os.SEEK_END)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            state["cursor"] = EventCursor(sequence, event["event_id"], event["event_hash"])
            state["last_event"] = event
            state["offset"] = size + len(encoded)
            self._event_states[run_id] = state
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return event

    def replay(self, run_id):
        return replay_events(self.read_events(run_id))

    def verify_event_cursor(self, run_id, sequence, event_hash):
        sequence = int(sequence)
        if sequence == 0:
            return str(event_hash) == EventCursor().event_hash
        events = self.read_events(run_id)
        return len(events) >= sequence and events[sequence - 1]["event_hash"] == str(event_hash)

    def operation_receipt(self, run_id, call_id):
        return self.replay(run_id).operation_receipt(call_id)

    def append_context(self, run_id, entry):
        path = self.context_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"run_id": _run_id(run_id), "record_type": "context_entry", "payload": entry}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return path

    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        return path

    def load_task_state(self, task_id):
        return json.loads(self.task_state_path(task_id).read_text(encoding="utf-8"))

    def load_report(self, task_id):
        return json.loads(self.report_path(task_id).read_text(encoding="utf-8"))

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。
        # 这样即使中途异常，也不容易留下半截 JSON。
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)
