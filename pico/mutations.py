"""Revision-bound atomic text mutations."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from .contracts import ToolFailureError
from .persistence import atomic_replace_bytes

ABSENT_REVISION = "absent"
MAX_TEXT_FILE_BYTES = 2_000_000
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def content_revision(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_revision(path: Path) -> str:
    if not path.is_file():
        return ABSENT_REVISION
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


class RevisionConflict(ToolFailureError):
    def __init__(self, path, expected, actual):
        super().__init__(
            "revision_conflict",
            f"revision conflict for {path}: expected {expected}, actual {actual}; read the file again",
        )


class TextNotFound(ToolFailureError):
    def __init__(self):
        super().__init__(
            "text_not_found",
            "old_text was not found; read the current file and choose a current exact block",
        )


class AmbiguousTextMatch(ToolFailureError):
    def __init__(self, count):
        super().__init__(
            "ambiguous_text_match",
            f"old_text matched {int(count)} locations; use a longer unique block",
        )


def _workspace_lock(root: Path):
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class WorkspaceMutationService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._lock = _workspace_lock(self.root)

    def _target(self, path):
        target = Path(path).resolve()
        if os.path.commonpath([str(self.root), str(target)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {path}")
        return target

    @staticmethod
    def _check_payload(payload):
        if len(payload) > MAX_TEXT_FILE_BYTES:
            raise ValueError(f"text mutation exceeds {MAX_TEXT_FILE_BYTES} bytes")

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes):
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        atomic_replace_bytes(path, payload, mode=mode)

    def write(self, path, content, expected_revision):
        target = self._target(path)
        payload = str(content).encode("utf-8")
        self._check_payload(payload)
        with self._lock:
            actual = file_revision(target)
            if actual != expected_revision:
                raise RevisionConflict(target.relative_to(self.root), expected_revision, actual)
            after = content_revision(payload)
            if actual != after:
                self._atomic_replace(target, payload)
        return actual, after

    def edit(self, path, old_text, new_text, expected_revision):
        target = self._target(path)
        with self._lock:
            if not target.is_file():
                raise ValueError("patch target is not a file")
            raw = target.read_bytes()
            actual = content_revision(raw)
            if actual != expected_revision:
                raise RevisionConflict(target.relative_to(self.root), expected_revision, actual)
            text = raw.decode("utf-8")
            count = text.count(str(old_text))
            if count == 0:
                raise TextNotFound()
            if count > 1:
                raise AmbiguousTextMatch(count)
            payload = text.replace(str(old_text), str(new_text), 1).encode("utf-8")
            self._check_payload(payload)
            after = content_revision(payload)
            if actual != after:
                self._atomic_replace(target, payload)
        return actual, after
