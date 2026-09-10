"""Atomic persistence for the single Session snapshot."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .persistence import atomic_write_json
from .session import Session
from .session_validation import validate_session

SESSION_SCHEMA_VERSION = "pico-session-snapshot-v1"
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

    def create(self, workspace_root, *, session_id=None):
        session = Session.create(self, workspace_root, session_id=session_id)
        self.directory(session.id).mkdir(parents=True, exist_ok=False)
        self.save(session)
        return session

    def save(self, session):
        payload = {"schema_version": SESSION_SCHEMA_VERSION, **session.to_dict()}
        self._validate(payload)
        return atomic_write_json(self.path(session.id), payload)

    def load(self, session_id, workspace_root=None):
        value = json.loads(self.path(session_id).read_text(encoding="utf-8"))
        self._validate(value)
        if value["id"] != session_id:
            raise ValueError("Session identity does not match its path")
        if workspace_root is not None and value["workspace_root"] != str(
            Path(workspace_root).resolve()
        ):
            raise ValueError("session belongs to another workspace")
        fields = {key: item for key, item in value.items() if key != "schema_version"}
        return Session(store=self, **fields)

    @staticmethod
    def _validate(value):
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
