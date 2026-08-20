"""Session shape, working memory, summaries, and persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from .features import memory as memorylib
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
        self.data = session or self._new_session()
        self.ensure_shape()
        self.memory = memorylib.SessionWorkingMemory(
            self.data.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=workspace_root,
        )
        self.data["memory"] = self.memory.to_dict()
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
            "memory": memorylib.default_memory_state(),
        }

    def ensure_shape(self):
        if self.data.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise ValueError("unsupported session schema")
        self.data.setdefault("active_run_id", "")
        self.data.setdefault("memory", memorylib.default_memory_state())

    def save(self):
        self.path = self.store.save(self.data)
        return self.path

    def reset(self):
        self.data["active_run_id"] = ""
        self.data["memory"].clear()
        self.data["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.SessionWorkingMemory(
            self.data["memory"],
            workspace_root=self.workspace_root,
        )
        return self.save()
