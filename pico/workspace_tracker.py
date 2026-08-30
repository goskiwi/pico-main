"""Workspace identity, path boundaries, and Runtime-observed revisions."""

from __future__ import annotations

import os
from pathlib import Path

from .mutations import file_revision
from .workspace import WorkspaceContext

TOOL_INTERNAL_PATH_NAMES = frozenset({".git", ".pico"})


class WorkspaceTracker:
    def __init__(self, workspace: WorkspaceContext):
        self.context = workspace
        self.root = Path(workspace.repo_root).resolve()
        self.invocation_cwd = Path(workspace.cwd).resolve()

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
        changed = refreshed.state() != self.context.state()
        if force or changed:
            self.context = refreshed
        return force or changed

    def resolve_path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    def resolve_tool_path(self, raw_path):
        resolved = self.resolve_path(raw_path)
        relative = resolved.relative_to(self.root)
        if any(part in TOOL_INTERNAL_PATH_NAMES for part in relative.parts):
            raise ValueError(f"tool path targets internal workspace state: {raw_path}")
        return resolved
