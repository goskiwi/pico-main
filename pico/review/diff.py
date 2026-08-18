"""Trusted Git diff loading and unified-diff line provenance."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from .contracts import (
    MAX_INLINE_DIFF_CHARS,
    ReviewRequest,
    normalize_repository_path,
)

MAX_DIFF_BYTES = 1_000_000
MAX_CHANGED_FILES = 200
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^:+-]{0,199}$")
_HUNK_RE = re.compile(r"^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,([0-9]+))? @@")


class GitDiffError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiffFile:
    path: str
    patch: str
    added_lines: frozenset[int]


@dataclass(frozen=True)
class GitDiff:
    repository: str
    root: Path
    base_sha: str
    head_sha: str
    files: tuple[DiffFile, ...]

    @property
    def changed_files(self):
        return tuple(item.path for item in self.files)

    @property
    def text(self):
        return "".join(item.patch for item in self.files)

    def changed_lines(self):
        return {item.path: item.added_lines for item in self.files}

    def requests(self, max_chars=MAX_INLINE_DIFF_CHARS):
        max_chars = min(int(max_chars), MAX_INLINE_DIFF_CHARS)
        if max_chars < 256:
            raise ValueError("review diff chunk budget must be at least 256 characters")
        groups = []
        current = []
        current_size = 0
        for item in self.files:
            if len(item.patch) > max_chars:
                raise GitDiffError(
                    f"single-file patch exceeds the {max_chars}-character review limit: {item.path}"
                )
            if current and current_size + len(item.patch) > max_chars:
                groups.append(tuple(current))
                current = []
                current_size = 0
            current.append(item)
            current_size += len(item.patch)
        if current:
            groups.append(tuple(current))
        return tuple(
            ReviewRequest(
                repository=self.repository,
                base_sha=self.base_sha,
                head_sha=self.head_sha,
                changed_files=[item.path for item in group],
                diff="".join(item.patch for item in group),
            )
            for group in groups
        )


def _validate_ref(value, field):
    value = str(value or "").strip()
    if not _REF_RE.fullmatch(value) or value.startswith("-"):
        raise GitDiffError(f"invalid Git {field} revision")
    return value


def _git(root, args, *, timeout=20):
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitDiffError(f"Git command failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GitDiffError(detail or "Git command failed")
    return result.stdout


def _resolve_revision(root, value, field):
    value = _validate_ref(value, field)
    return _git(root, ["rev-parse", "--verify", f"{value}^{{commit}}"]).strip()


def _decode_diff_path(value):
    value = str(value).strip()
    if value == "/dev/null":
        return ""
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GitDiffError("invalid quoted path in Git diff") from exc
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return normalize_repository_path(value)


def _file_path(lines):
    old_path = ""
    for line in lines:
        if line.startswith("--- "):
            old_path = _decode_diff_path(line[4:].rstrip("\n"))
        elif line.startswith("+++ "):
            new_path = _decode_diff_path(line[4:].rstrip("\n"))
            return new_path or old_path
        elif line.startswith("rename to "):
            return normalize_repository_path(line[len("rename to ") :].rstrip("\n"))
    header = lines[0].rstrip("\n") if lines else ""
    try:
        parts = shlex.split(header)
    except ValueError:
        parts = []
    if len(parts) == 4 and parts[:2] == ["diff", "--git"]:
        return _decode_diff_path(parts[3])
    marker = " b/"
    if header.startswith("diff --git ") and marker in header:
        return _decode_diff_path(header.rsplit(marker, 1)[1])
    raise GitDiffError("Git diff file block has no usable path header")


def _added_lines(lines):
    added = set()
    new_line = None
    for line in lines:
        match = _HUNK_RE.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+"):
            added.add(new_line)
            new_line += 1
        elif not line.startswith("-"):
            new_line += 1
    return frozenset(added)


def parse_git_diff(text):
    lines = str(text or "").splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts:
        return ()
    starts.append(len(lines))
    files = []
    for start, end in pairwise(starts):
        block = lines[start:end]
        files.append(
            DiffFile(
                path=_file_path(block),
                patch="".join(block),
                added_lines=_added_lines(block),
            )
        )
    return tuple(files)


def parse_added_lines(text):
    return {item.path: item.added_lines for item in parse_git_diff(text)}


def load_git_diff(repository, base, head):
    root = Path(repository).expanduser().resolve()
    if not root.is_dir():
        raise GitDiffError(f"repository does not exist: {root}")
    repo_root = Path(_git(root, ["rev-parse", "--show-toplevel"]).strip()).resolve()
    base_sha = _resolve_revision(repo_root, base, "base")
    head_sha = _resolve_revision(repo_root, head, "head")
    if base_sha == head_sha:
        raise GitDiffError("base and head resolve to the same commit")
    current_head = _git(repo_root, ["rev-parse", "HEAD"]).strip()
    if current_head != head_sha:
        raise GitDiffError("repository HEAD must match the reviewed head revision")
    if _git(repo_root, ["status", "--porcelain", "--untracked-files=all"]).strip():
        raise GitDiffError("repository worktree must be clean for review")
    text = _git(
        repo_root,
        ["diff", "--no-ext-diff", "--unified=3", "--find-renames", base_sha, head_sha, "--"],
        timeout=60,
    )
    if len(text.encode()) > MAX_DIFF_BYTES:
        raise GitDiffError("Git diff exceeds the one-megabyte loader limit")
    files = parse_git_diff(text)
    if not files:
        raise GitDiffError("reviewed revisions contain no file changes")
    if len(files) > MAX_CHANGED_FILES:
        raise GitDiffError("Git diff changes too many files")
    return GitDiff(
        repository=repo_root.name,
        root=repo_root,
        base_sha=base_sha,
        head_sha=head_sha,
        files=files,
    )
