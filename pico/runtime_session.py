"""Session identity and active Run pointer persistence."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .session_store import SESSION_SCHEMA_VERSION, SessionStore
from .workspace import now


class RuntimeSession:
    def __init__(
        self,
        store: SessionStore,
        workspace_root: Path,
        session: dict | None = None,
    ):
        self.store = store
        self.workspace_root = workspace_root
        self.data = deepcopy(session) if session is not None else self._new_session()
        self.ensure_shape()
        self.path = None

    def _new_session(self):
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-"
            + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": str(self.workspace_root),
            "active_run_id": "",
        }

    def ensure_shape(self):
        self.store.validate(self.data)

    def _commit(self, candidate):
        candidate = deepcopy(candidate)
        path = self.store.save(candidate)
        self.data = candidate
        self.path = path
        return path

    def save(self):
        return self._commit(self.data)

    def set_active_run(self, run_id):
        candidate = deepcopy(self.data)
        candidate["active_run_id"] = str(run_id)
        return self._commit(candidate)

    def reset(self):
        candidate = deepcopy(self.data)
        candidate["active_run_id"] = ""
        return self._commit(candidate)
