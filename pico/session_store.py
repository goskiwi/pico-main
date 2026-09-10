"""Append completed conversation units, then atomically checkpoint execution state."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .persistence import atomic_write_json
from .session import Session
from .session_validation import validate_session

SESSION_SCHEMA_VERSION = "pico-session-transcript-v1"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class SessionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, session_id):
        session_id = str(session_id)
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")
        path = self.root / session_id
        if path.is_symlink():
            raise ValueError("session directory must not be a symlink")
        return path

    def path(self, session_id):
        path = self.directory(session_id) / "session.json"
        if path.is_symlink():
            raise ValueError("session path must not be a symlink")
        return path

    def artifact_dir(self, session_id):
        path = self.directory(session_id) / "artifacts"
        if path.is_symlink():
            raise ValueError("artifact directory must not be a symlink")
        return path

    def transcript_path(self, session_id):
        path = self.directory(session_id) / "messages.jsonl"
        if path.is_symlink():
            raise ValueError("transcript must not be a symlink")
        return path

    def create(self, workspace_root, *, session_id=None):
        session = Session.create(self, workspace_root, session_id=session_id)
        self.directory(session.id).mkdir(parents=True, exist_ok=False)
        self.save(session)
        return session

    def save(self, session):
        payload = {"schema_version": SESSION_SCHEMA_VERSION, **session.to_dict()}
        self.validate(payload)
        # Only completed history units are immutable. An unfinished tool batch
        # stays in the checkpoint, including its phases and pre-write plans.
        end = session._stored_count
        while end < len(session.history):
            entry = session.history[end]
            if entry["kind"] == "tool_turn" and any(
                phase != "finished" for phase in entry["phases"].values()
            ):
                break
            end += 1
        path = self.transcript_path(session.id)
        records = []
        for entry in session.history[session._stored_count:end]:
            public = {key: value for key, value in entry.items() if key not in {"phases", "plans"}}
            records.append(json.dumps(public, ensure_ascii=False) + "\n")
        with path.open("r+b" if path.exists() else "w+b") as handle:
            os.fchmod(handle.fileno(), 0o600)
            if os.fstat(handle.fileno()).st_size < session._stored_bytes:
                raise ValueError("committed transcript was truncated")
            # A previous interrupted save may have left a complete or torn tail.
            # The checkpoint owns the commit position; never adopt that tail.
            handle.truncate(session._stored_bytes)
            handle.seek(session._stored_bytes)
            handle.write("".join(records).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            committed_bytes = handle.tell()
        payload.pop("history")
        payload.update(history_count=end, transcript_bytes=committed_bytes,
                       pending_history=session.history[end:])
        result = atomic_write_json(self.path(session.id), payload)
        session._stored_count, session._stored_bytes = end, committed_bytes
        return result

    def load(self, session_id, workspace_root=None):
        value = json.loads(self.path(session_id).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("session checkpoint must be an object")
        if value.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise ValueError("unsupported session schema")
        if not {"history_count", "transcript_bytes", "pending_history"} <= value.keys():
            raise ValueError("missing transcript commit position")
        count = value.pop("history_count")
        size = value.pop("transcript_bytes")
        pending = value.pop("pending_history")
        if type(count) is not int or count < 0 or type(size) is not int or size < 0:
            raise ValueError("invalid transcript commit position")
        path = self.transcript_path(session_id)
        with path.open("rb") as handle:
            data = handle.read(size)
        if len(data) != size or (data and not data.endswith(b"\n")):
            raise ValueError("incomplete committed transcript")
        history = [json.loads(line) for line in data.splitlines()]
        if len(history) != count:
            raise ValueError("transcript record count mismatch")
        for entry in history:
            if entry.get("kind") == "tool_turn":
                entry["phases"] = {call["call_id"]: "finished" for call in entry["calls"]}
        if not isinstance(pending, list):
            raise TypeError("invalid pending history")
        value["history"] = history + pending
        self.validate(value)
        if value["id"] != session_id:
            raise ValueError("Session identity does not match its path")
        if workspace_root is not None and value["workspace_root"] != str(
            Path(workspace_root).resolve()
        ):
            raise ValueError("session belongs to another workspace")
        fields = {key: item for key, item in value.items() if key != "schema_version"}
        # Trim only bytes beyond the committed checkpoint after validating it.
        with path.open("r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())
        return Session(store=self, _stored_count=count, _stored_bytes=size, **fields)

    @staticmethod
    def validate(value):
        validate_session(value, schema_version=SESSION_SCHEMA_VERSION)
        if not SESSION_ID.fullmatch(str(value["id"])):
            raise ValueError("invalid session id")

    def latest_active(self):
        candidates = []
        if not self.root.exists():
            return None
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            path = directory / "session.json"
            if not path.is_file() or path.is_symlink():
                continue
            try:
                session = self.load(directory.name)
            except (OSError, ValueError, TypeError):
                continue
            if session.run.get("status") not in {"completed", "reset"}:
                candidates.append((path.stat().st_mtime_ns, session.id))
        return max(candidates)[1] if candidates else None


__all__ = ["Session", "SessionStore"]
