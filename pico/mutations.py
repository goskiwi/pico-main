"""Exact, atomic text mutations for the current workspace state."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import ToolFailureError
from .persistence import atomic_replace_bytes, write_once_bytes

ABSENT_REVISION = "absent"
MAX_DIAGNOSTIC_LOCATIONS = 8
MAX_DIAGNOSTIC_EXCERPT_CHARS = 2000
DIAGNOSTIC_CONTEXT_LINES = 5


@dataclass(frozen=True)
class MutationReceipt:
    before_revision: str
    after_revision: str
    diff: str

    @property
    def changed(self):
        return self.before_revision != self.after_revision


@dataclass(frozen=True)
class PreparedEdit:
    """A validated replacement prepared from the file's current bytes."""

    target: Path
    payload: bytes
    mode: int
    receipt: MutationReceipt


def content_revision(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def file_revision(path: Path, *, execution_context=None) -> str:
    if not path.is_file():
        return ABSENT_REVISION
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if execution_context is not None:
                execution_context.check_active()
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _match_locations(text, old_text):
    count = text.count(old_text)
    locations = []
    offset = 0
    line_span = max(1, len(str(old_text).splitlines()))
    for _index in range(min(count, MAX_DIAGNOSTIC_LOCATIONS)):
        offset = text.find(old_text, offset)
        if offset < 0:
            break
        start_line = text.count("\n", 0, offset) + 1
        locations.append(
            {
                "start_line": start_line,
                "end_line": start_line + line_span - 1,
            }
        )
        offset += max(1, len(old_text))
    return count, locations


def _read_args(path, start_line, end_line, total_lines):
    return {
        "path": Path(path).as_posix(),
        "start_line": max(1, int(start_line) - DIAGNOSTIC_CONTEXT_LINES),
        "end_line": min(
            max(1, int(total_lines)),
            int(end_line) + DIAGNOSTIC_CONTEXT_LINES,
        ),
    }


def _closest_match(text, old_text):
    current_lines = text.splitlines()
    search_lines = str(old_text).splitlines()
    if not current_lines or not search_lines:
        return None

    matcher = difflib.SequenceMatcher(
        None,
        search_lines,
        current_lines,
        autojunk=False,
    )
    block = max(matcher.get_matching_blocks(), key=lambda item: item.size)
    if block.size:
        start = max(0, block.b - block.a)
    else:
        anchor = search_lines[0]
        ratios = [
            difflib.SequenceMatcher(None, anchor, line).ratio()
            for line in current_lines
        ]
        start = max(range(len(ratios)), key=ratios.__getitem__)

    end = min(len(current_lines), start + max(1, len(search_lines)))
    candidate = current_lines[start:end]
    similarity = difflib.SequenceMatcher(
        None,
        "\n".join(search_lines),
        "\n".join(candidate),
        autojunk=False,
    ).ratio()
    if similarity < 0.35:
        return None
    content = "\n".join(candidate)
    if len(content) > MAX_DIAGNOSTIC_EXCERPT_CHARS:
        content = content[: MAX_DIAGNOSTIC_EXCERPT_CHARS].rstrip() + " …"
    return {
        "start_line": start + 1,
        "end_line": max(start + 1, end),
        "similarity": round(similarity, 3),
        "content": content,
    }


class TextNotFound(ToolFailureError):
    def __init__(self, path, revision, *, current_text, old_text):
        logical_path = Path(path).as_posix()
        closest = _closest_match(current_text, old_text)
        first = closest or {"start_line": 1, "end_line": 200}
        super().__init__(
            "text_not_found",
            "old_text was not found; inspect structured.closest_match when present, read the recommended range, and choose an exact block",
            structured={
                "path": logical_path,
                "actual_revision": str(revision),
                "match_count": 0,
                "closest_match": closest,
                "recommended_next_tool": "read_file",
                "recommended_tool_args": _read_args(
                    logical_path,
                    first["start_line"],
                    first["end_line"],
                    len(current_text.splitlines()),
                ),
            },
        )


class AmbiguousTextMatch(ToolFailureError):
    def __init__(self, path, revision, *, current_text, old_text):
        logical_path = Path(path).as_posix()
        count, locations = _match_locations(current_text, old_text)
        first = locations[0]
        super().__init__(
            "ambiguous_text_match",
            f"old_text matched {int(count)} locations; use a longer unique block",
            structured={
                "path": logical_path,
                "actual_revision": str(revision),
                "match_count": int(count),
                "match_locations": locations,
                "match_locations_truncated": count > len(locations),
                "recommended_next_tool": "read_file",
                "recommended_tool_args": _read_args(
                    logical_path,
                    first["start_line"],
                    first["end_line"],
                    len(current_text.splitlines()),
                ),
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


class WorkspaceMutationService:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _target(self, path):
        target = Path(path).resolve()
        if target != Path(path):
            raise ToolFailureError(
                "mutation_target_changed",
                "prepared mutation target changed; resolve and authorize again",
                recovery="retry_after_change",
            )
        if os.path.commonpath([str(self.root), str(target)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {path}")
        return target

    def write(self, path, content):
        target = self._target(path)
        logical_path = target.relative_to(self.root)
        payload = str(content).encode("utf-8")
        if not write_once_bytes(target, payload, mode=0o644):
            raise ExistingFileRequiresEdit(
                logical_path,
                file_revision(target),
            )
        after = content_revision(payload)
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

    def prepare_edit(self, path, old_text, new_text, *, execution_context):
        """Build one exact replacement from the file's current contents."""
        target = self._target(path)
        logical_path = target.relative_to(self.root)
        execution_context.check_active()
        if not target.is_file():
            raise ValueError("patch target is not a file")
        mode = target.stat().st_mode & 0o777
        raw = target.read_bytes()
        execution_context.check_active()
        before = content_revision(raw)
        text = raw.decode("utf-8")
        # read_file presents LF text. Match those logical line breaks against
        # the current bytes, without normalizing the entire file on write.
        old_text = str(old_text).replace("\r\n", "\n")
        new_text = str(new_text).replace("\r\n", "\n")
        pattern = re.compile(
            r"\r?\n".join(re.escape(line) for line in old_text.split("\n"))
        )
        match = pattern.search(text)
        if match is None:
            raise TextNotFound(
                logical_path,
                before,
                current_text=text.replace("\r\n", "\n"),
                old_text=old_text,
            )
        if pattern.search(text, match.end()) is not None:
            raise AmbiguousTextMatch(
                logical_path,
                before,
                current_text=text.replace("\r\n", "\n"),
                old_text=old_text,
            )
        payload = raw
        if old_text != new_text:
            ending = re.search(r"\r?\n", text[match.start():]) or re.search(
                r"\r?\n", text
            )
            newline = ending.group() if ending is not None else "\n"
            replacement = new_text.replace("\n", newline)
            payload = (
                text[:match.start()] + replacement + text[match.end():]
            ).encode("utf-8")
        after = content_revision(payload)
        receipt = MutationReceipt(
            before_revision=before,
            after_revision=after,
            diff=(
                unified_text_diff(logical_path, text, payload.decode("utf-8"))
                if before != after
                else ""
            ),
        )
        return PreparedEdit(target, payload, mode, receipt)

    @staticmethod
    def commit_edit(prepared: PreparedEdit):
        """Publish a prepared replacement without another content read."""
        if prepared.receipt.changed:
            atomic_replace_bytes(
                prepared.target,
                prepared.payload,
                mode=prepared.mode,
            )
        return prepared.receipt
