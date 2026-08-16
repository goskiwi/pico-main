"""Revision-bound atomic text mutations."""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from pathlib import Path

ABSENT_REVISION = "absent"
MAX_TEXT_FILE_BYTES = 2_000_000
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def content_revision(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_revision(path: Path) -> str:
    return content_revision(path.read_bytes()) if path.is_file() else ABSENT_REVISION


class RevisionConflict(RuntimeError):
    def __init__(self, path, expected, actual):
        super().__init__(f"revision conflict for {path}: expected {expected}, actual {actual}; read the file again")
        self.path = str(path)
        self.expected_revision = str(expected)
        self.actual_revision = str(actual)


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
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.chmod(mode)
        temporary.replace(path)

    def write(self, path, content, expected_revision):
        target = self._target(path)
        payload = str(content).encode("utf-8")
        self._check_payload(payload)
        with self._lock:
            actual = file_revision(target)
            if actual != expected_revision:
                raise RevisionConflict(target.relative_to(self.root), expected_revision, actual)
            self._atomic_replace(target, payload)
        return actual, content_revision(payload)

    def patch(self, path, old_text, new_text, expected_revision):
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
            if count != 1:
                raise ValueError(f"old_text must occur exactly once, found {count}")
            payload = text.replace(str(old_text), str(new_text), 1).encode("utf-8")
            self._check_payload(payload)
            self._atomic_replace(target, payload)
        return actual, content_revision(payload)
