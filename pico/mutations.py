"""Revision-bound atomic text mutations."""

from __future__ import annotations

import difflib
import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .contracts import ToolFailureError
from .persistence import atomic_replace_bytes

ABSENT_REVISION = "absent"
MAX_TEXT_FILE_BYTES = 2_000_000
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class MutationReceipt:
    before_revision: str
    after_revision: str
    diff: str

    @property
    def changed(self):
        return self.before_revision != self.after_revision


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
        logical_path = Path(path).as_posix()
        super().__init__(
            "revision_conflict",
            f"revision conflict for {logical_path}: expected {expected}, actual {actual}; read the file again",
            structured={
                "path": logical_path,
                "expected_revision": str(expected),
                "actual_revision": str(actual),
                "recommended_next_tool": "read_file",
            },
        )


class TextNotFound(ToolFailureError):
    def __init__(self, path, revision):
        logical_path = Path(path).as_posix()
        super().__init__(
            "text_not_found",
            "old_text was not found; read the current file and choose a current exact block",
            structured={
                "path": logical_path,
                "actual_revision": str(revision),
                "match_count": 0,
                "recommended_next_tool": "read_file",
            },
        )


class AmbiguousTextMatch(ToolFailureError):
    def __init__(self, path, revision, count):
        logical_path = Path(path).as_posix()
        super().__init__(
            "ambiguous_text_match",
            f"old_text matched {int(count)} locations; use a longer unique block",
            structured={
                "path": logical_path,
                "actual_revision": str(revision),
                "match_count": int(count),
                "recommended_next_tool": "read_file",
            },
        )


class ExistingFileRequiresEdit(ToolFailureError):
    def __init__(self, path, revision):
        logical_path = Path(path).as_posix()
        super().__init__(
            "existing_file_requires_edit",
            "write_file only creates new files; read the current file and use edit_file",
            structured={
                "path": logical_path,
                "actual_revision": str(revision),
                "recommended_next_tool": "read_file",
            },
        )


def unified_text_diff(path, before, after, *, before_exists=True, after_exists=True):
    logical_path = Path(path).as_posix()
    if not before_exists and not str(after):
        return f"--- /dev/null\n+++ b/{logical_path}\n"
    lines = difflib.unified_diff(
        str(before).splitlines(keepends=True),
        str(after).splitlines(keepends=True),
        fromfile=f"a/{logical_path}" if before_exists else "/dev/null",
        tofile=f"b/{logical_path}" if after_exists else "/dev/null",
        lineterm="\n",
    )
    rendered = []
    for line in lines:
        rendered.append(line)
        if not line.endswith(("\n", "\r")):
            rendered.append("\n\\ No newline at end of file\n")
    return "".join(rendered)


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

    def _require_revision(self, target, logical_path, expected_revision):
        actual = file_revision(target)
        if actual != expected_revision:
            raise RevisionConflict(logical_path, expected_revision, actual)
        return actual

    def _commit(self, target, logical_path, payload, expected_revision):
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        atomic_replace_bytes(
            target,
            payload,
            mode=mode,
            commit_guard=lambda: self._require_revision(
                target,
                logical_path,
                expected_revision,
            ),
        )

    def write(self, path, content):
        target = self._target(path)
        logical_path = target.relative_to(self.root)
        payload = str(content).encode("utf-8")
        self._check_payload(payload)
        with self._lock:
            actual = file_revision(target)
            if actual != ABSENT_REVISION:
                raise ExistingFileRequiresEdit(logical_path, actual)
            after = content_revision(payload)
            self._commit(target, logical_path, payload, ABSENT_REVISION)
        return MutationReceipt(
            before_revision=ABSENT_REVISION,
            after_revision=after,
            diff=unified_text_diff(
                logical_path,
                "",
                payload.decode("utf-8"),
                before_exists=False,
            ),
        )

    def edit(self, path, old_text, new_text, expected_revision):
        target = self._target(path)
        logical_path = target.relative_to(self.root)
        with self._lock:
            if not target.is_file():
                raise ValueError("patch target is not a file")
            raw = target.read_bytes()
            actual = content_revision(raw)
            if actual != expected_revision:
                raise RevisionConflict(logical_path, expected_revision, actual)
            text = raw.decode("utf-8")
            count = text.count(str(old_text))
            if count == 0:
                raise TextNotFound(logical_path, actual)
            if count > 1:
                raise AmbiguousTextMatch(logical_path, actual, count)
            payload = text.replace(str(old_text), str(new_text), 1).encode("utf-8")
            self._check_payload(payload)
            after = content_revision(payload)
            if actual != after:
                self._commit(target, logical_path, payload, expected_revision)
        return MutationReceipt(
            before_revision=actual,
            after_revision=after,
            diff=(
                unified_text_diff(logical_path, text, payload.decode("utf-8"))
                if actual != after
                else ""
            ),
        )
