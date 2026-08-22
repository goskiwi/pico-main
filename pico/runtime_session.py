"""Session identity and active Run pointer persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from .session_store import SESSION_SCHEMA_VERSION, SessionStore


class RuntimeSession:
    def __init__(
        self,
        store: SessionStore,
        workspace_root: Path,
        session: dict | None = None,
    ):
        self.store = store
        self.workspace_root = workspace_root
        self.data = dict(session) if session is not None else self._new_session()
        self.store.validate(self.data)
        self.path = None

    def _new_session(self):
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6],
            "workspace_root": str(self.workspace_root),
            "active_run_id": "",
        }

    def _commit(self, candidate):
        candidate = dict(candidate)
        path = self.store.save(candidate)
        self.data = candidate
        self.path = path
        return path

    def save(self):
        return self._commit(self.data)

    def set_active_run(self, run_id):
        candidate = dict(self.data)
        candidate["active_run_id"] = str(run_id)
        return self._commit(candidate)

    def reset(self):
        candidate = dict(self.data)
        candidate["active_run_id"] = ""
        return self._commit(candidate)
