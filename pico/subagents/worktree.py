"""Trusted Git worktrees and patch operations for implementation children."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..persistence import write_once_bytes
from ..workspace import normalize_relative_file


class GitWorktreeError(RuntimeError):
    pass


def _git(root, *args, input_bytes=None, execution_context=None):
    timeout = execution_context.bounded_timeout(10) if execution_context else 10
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=Path(root), input=input_bytes, capture_output=True,
            check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitWorktreeError(f"Git command failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise GitWorktreeError(detail.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def require_clean_repository(root, *, execution_context=None):
    root = Path(root).resolve()
    try:
        top = Path(
            _git(root, "rev-parse", "--show-toplevel", execution_context=execution_context)
            .decode("utf-8")
            .strip()
        ).resolve()
    except GitWorktreeError as exc:
        raise GitWorktreeError("implementation subtasks require a Git repository") from exc
    if top != root:
        raise GitWorktreeError("implementation subtasks must run from the repository root")
    tracked_status = _git(
        root,
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=no",
        execution_context=execution_context,
    )
    untracked = tuple(
        path
        for path in _git(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            execution_context=execution_context,
        ).split(b"\0")
        if path and path != b".pico" and not path.startswith(b".pico/")
    )
    if tracked_status or untracked:
        raise GitWorktreeError(
            "implementation subtasks require a clean working tree before delegation"
        )
    return _git(root, "rev-parse", "HEAD", execution_context=execution_context).decode("utf-8").strip()


def repository_changed_paths(root, *, execution_context=None):
    tracked = _git(root, "diff", "--name-only", "-z", "HEAD", execution_context=execution_context)
    untracked = _git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        execution_context=execution_context,
    )
    return tuple(
        sorted(
            {
                normalize_relative_file(raw.decode("utf-8"))
                for raw in (tracked + untracked).split(b"\0")
                if raw and raw != b".pico" and not raw.startswith(b".pico/")
            }
        )
    )


@dataclass
class GitWorktree:
    repository_root: Path
    base_sha: str
    label: str
    container_root: Path | None = None
    path: Path | None = None
    execution_context: object | None = None

    def create(self):
        if self.path is not None:
            return self.path
        self.container_root = Path(tempfile.mkdtemp(prefix="pico-subagent-"))
        self.path = self.container_root / self.label
        try:
            _git(
                self.repository_root,
                "worktree",
                "add",
                "--detach",
                str(self.path),
                self.base_sha,
                execution_context=self.execution_context,
            )
        except Exception:
            self.cleanup()
            raise
        return self.path

    def apply_patch(self, patch):
        apply_patch(self.path, patch, execution_context=self.execution_context)

    def changed_paths(self):
        return repository_changed_paths(self.path, execution_context=self.execution_context)

    def patch(self):
        changed = self.changed_paths()
        if not changed:
            return b""
        untracked = {
            item.decode("utf-8")
            for item in _git(
                self.path,
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                execution_context=self.execution_context,
            ).split(b"\0")
            if item
        }
        if untracked:
            _git(self.path, "add", "-N", "--", *sorted(untracked), execution_context=self.execution_context)
        return _git(self.path, "diff", "--binary", "--no-ext-diff", "HEAD", execution_context=self.execution_context)

    def write_patch(self, destination):
        payload = self.patch()
        if not payload:
            raise GitWorktreeError("implementation subtask produced no patch")
        destination = Path(destination)
        if (
            not write_once_bytes(destination, payload)
            and destination.read_bytes() != payload
        ):
            raise GitWorktreeError(
                f"immutable subtask patch collision: {destination.name}"
            )
        return hashlib.sha256(payload).hexdigest()

    def cleanup(self):
        if self.path is not None and self.path.exists():
            try:
                _git(self.repository_root, "worktree", "remove", "--force", str(self.path))
            except (GitWorktreeError, OSError):
                pass
        if self.container_root is not None:
            shutil.rmtree(self.container_root, ignore_errors=True)
        self.path = None
        self.container_root = None


def apply_patch(root, patch, *, execution_context=None):
    payload = bytes(patch)
    _git(root, "apply", "--check", "--whitespace=nowarn", "-", input_bytes=payload, execution_context=execution_context)
    _git(root, "apply", "--whitespace=nowarn", "-", input_bytes=payload, execution_context=execution_context)
