"""Workspace identity, path boundary, snapshots, and content fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .mutations import file_revision
from .workspace import IGNORED_PATH_NAMES, WorkspaceContext


class WorkspaceTracker:
    def __init__(self, workspace: WorkspaceContext):
        self.context = workspace
        self.root = Path(workspace.repo_root)
        self.invocation_cwd = Path(workspace.cwd)
        self._snapshot_cache: dict[str, str] | None = None
        self._content_fingerprint_cache: str | None = None
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def mark_changed(self) -> None:
        """Invalidate derived state after a Runtime-observed workspace mutation."""
        self._revision += 1
        self._snapshot_cache = None
        self._content_fingerprint_cache = None

    @staticmethod
    def path_state(path) -> str:
        path = Path(path)
        try:
            if path.is_file():
                return file_revision(path)
            if path.is_dir():
                metadata = path.stat()
                return f"dir:{metadata.st_mtime_ns}:{metadata.st_ctime_ns}"
        except OSError:
            return "unavailable"
        return "absent"

    def refresh(self, *, force: bool = False) -> bool:
        refreshed = WorkspaceContext.build(
            self.invocation_cwd,
            repo_root_override=self.root,
        )
        changed = refreshed.fingerprint() != self.context.fingerprint()
        if changed:
            self.mark_changed()
        if force or changed:
            self.context = refreshed
        return force or changed

    def _scan_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(self.root).as_posix()
                snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:  # noqa: BLE001, S112 - files can move during a live scan
                continue
        return snapshot

    def capture_snapshot(self, *, force: bool = False) -> dict[str, str]:
        if force or self._snapshot_cache is None:
            self._snapshot_cache = self._scan_snapshot()
            self._content_fingerprint_cache = None
        return dict(self._snapshot_cache)

    def content_fingerprint(self, *, force: bool = False) -> str:
        if force:
            self.capture_snapshot(force=True)
        if self._content_fingerprint_cache is None:
            payload = json.dumps(
                self.capture_snapshot(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self._content_fingerprint_cache = hashlib.sha256(payload).hexdigest()
        return self._content_fingerprint_cache

    def resolve_path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved
