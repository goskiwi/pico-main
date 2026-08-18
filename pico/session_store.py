"""Strict, atomic Session persistence; run evidence lives elsewhere."""

import json
import re
from pathlib import Path

from .persistence import atomic_write_json

SESSION_SCHEMA_VERSION = "session-v7"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
RUN_SUMMARY_FIELDS = {
    "role",
    "run_id",
    "request",
    "content",
    "changed_paths",
    "verification_status",
    "stop_reason",
    "created_at",
}


def _validate_history(history):
    if not isinstance(history, list):
        raise TypeError("session history must be a list")
    for item in history:
        if not isinstance(item, dict):
            raise TypeError("session history entries must be objects")
        role = item.get("role")
        if role != "run_summary" or set(item) != RUN_SUMMARY_FIELDS:
            raise ValueError("unsupported session history entry")
        if not all(
            isinstance(item.get(field), str)
            for field in RUN_SUMMARY_FIELDS - {"changed_paths"}
        ):
            raise TypeError("session history text fields must be strings")
        if (
            not isinstance(item["changed_paths"], list)
            or any(not isinstance(path, str) for path in item["changed_paths"])
        ):
            raise TypeError("run summary changed_paths must be strings")


class SessionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        session_id = str(session_id)
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")
        path = self.root / f"{session_id}.json"
        if path.is_symlink():
            raise ValueError("session path must not be a symlink")
        return path

    @staticmethod
    def validate(session):
        if not isinstance(session, dict):
            raise TypeError("session must be an object")
        if session.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise ValueError("unsupported session schema")
        required = {"id", "created_at", "workspace_root", "history", "memory"}
        if not required.issubset(session):
            raise ValueError("session is missing required fields")
        _validate_history(session["history"])
        if not isinstance(session["memory"], dict):
            raise TypeError("invalid session state")

    def save(self, session):
        self.validate(session)
        path = self.path(session["id"])
        atomic_write_json(path, session)
        return path

    def load(self, session_id):
        path = self.path(session_id)
        session = json.loads(path.read_text(encoding="utf-8"))
        self.validate(session)
        return session

    def latest(self):
        files = [path for path in self.root.glob("*.json") if not path.is_symlink()]
        # Some mounted/container filesystems expose identical timestamps for
        # adjacent atomic writes. The session id is the deterministic tie-breaker.
        files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
        return files[-1].stem if files else None
