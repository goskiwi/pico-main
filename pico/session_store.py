"""Strict, atomic Session persistence; run evidence lives elsewhere."""

import json
import re
import tempfile
from pathlib import Path

SESSION_SCHEMA_VERSION = "session-v5"
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
        required = {"id", "created_at", "workspace_root", "history", "memory"}
        if not required.issubset(session):
            raise ValueError("session is missing required fields")
        if not isinstance(session["history"], list) or not isinstance(session["memory"], dict):
            raise TypeError("invalid session state")

    def save(self, session):
        self.validate(session)
        path = self.path(session["id"])
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=self.root, prefix=path.name + ".", suffix=".tmp"
        ) as handle:
            json.dump(session, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)
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
