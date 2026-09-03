"""Strict, atomic Session persistence; run evidence lives elsewhere."""

import json
import re
from pathlib import Path

from .persistence import atomic_write_json
from .run_store import RUN_ID

SESSION_SCHEMA_VERSION = "session-v11"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


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
        required = {
            "schema_version",
            "id",
            "workspace_root",
            "active_run_id",
        }
        if set(session) != required:
            raise ValueError("invalid session fields")
        if not isinstance(session["id"], str) or not SESSION_ID.fullmatch(
            session["id"]
        ):
            raise ValueError("invalid session id")
        if not isinstance(session["active_run_id"], str):
            raise TypeError("session active_run_id must be a string")
        if session["active_run_id"] and not RUN_ID.fullmatch(
            session["active_run_id"]
        ):
            raise ValueError("invalid session active_run_id")

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

    def latest_active(self):
        """Return the newest Session that still points at an unfinished Run."""

        files = [path for path in self.root.glob("*.json") if not path.is_symlink()]
        files.sort(
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for path in files:
            session = self.load(path.stem)
            if session["active_run_id"]:
                return path.stem
        return None
