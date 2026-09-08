"""Strict, atomic Session persistence; run evidence lives elsewhere."""

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .persistence import atomic_write_json
from .run_store import RUN_ID, RunStore

SESSION_SCHEMA_VERSION = "session-v12"
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


@dataclass
class Session:
    store: "SessionStore"
    id: str
    workspace_root: Path
    active_run_id: str = ""

    @property
    def path(self):
        return self.store.path(self.id)

    def set_active_run(self, run_id):
        candidate = replace(self, active_run_id=str(run_id))
        path = self.store.save(candidate)
        self.active_run_id = candidate.active_run_id
        return path


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

    def runs(self, session_id, *, trace=None):
        root = self.directory(session_id) / "runs"
        if root.is_symlink():
            raise ValueError("session runs directory must not be a symlink")
        return RunStore(root, trace=trace)

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
        payload = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": session.id,
            "workspace_root": str(session.workspace_root),
            "active_run_id": session.active_run_id,
        }
        self.validate(payload)
        path = self.path(session.id)
        atomic_write_json(path, payload)
        return path

    def create(self, workspace_root, *, session_id=None):
        session_id = session_id if session_id is not None else (
            datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-" + uuid.uuid4().hex[:6]
        )
        # New means new: never overwrite a Session or adopt orphaned state.
        self.directory(session_id).mkdir(parents=True, exist_ok=False)
        session = Session(
            self,
            session_id,
            Path(workspace_root).resolve(),
        )
        self.save(session)
        return session

    def load(self, session_id):
        path = self.path(session_id)
        session = json.loads(path.read_text(encoding="utf-8"))
        self.validate(session)
        return Session(
            self, session["id"], Path(session["workspace_root"]), session["active_run_id"]
        )

    def latest_active(self):
        """Return the Session with the newest pointed or orphaned unfinished Run."""

        directories = [path for path in self.root.iterdir()
                       if path.is_dir() and not path.is_symlink()
                       and (path / "session.json").is_file()]
        candidates = []
        for path in directories:
            session = self.load(path.name)
            run_store = self.runs(session.id)
            run_log = None
            if session.active_run_id:
                run_log = run_store.load_run(session.active_run_id)
                if run_log.projection.session_id != session.id:
                    raise ValueError("active Run does not belong to this Session")
                if run_log.projection.terminal:
                    session.set_active_run("")
                    run_log = run_store.find_active_run(session.id)
            else:
                run_log = run_store.find_active_run(session.id)
            if run_log is None:
                continue
            candidates.append(
                (
                    run_log.events[-1].timestamp,
                    run_log.run_id,
                    session.id,
                    session,
                    run_log,
                )
            )
        if not candidates:
            return None
        _timestamp, _run_id, _session_id, session, run_log = max(
            candidates,
            key=lambda item: item[:3],
        )
        if session.active_run_id != run_log.run_id:
            session.set_active_run(run_log.run_id)
        return session.id
