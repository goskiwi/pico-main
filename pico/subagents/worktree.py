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


def _git(root, *args, input_bytes=None):
    result = subprocess.run(
        ["git", *args],
        cwd=Path(root),
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise GitWorktreeError(detail.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def require_clean_repository(root):
    root = Path(root).resolve()
    try:
        top = Path(
            _git(root, "rev-parse", "--show-toplevel")
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
    )
    untracked = tuple(
        path
        for path in _git(
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).split(b"\0")
        if path and path != b".pico" and not path.startswith(b".pico/")
    )
    if tracked_status or untracked:
        raise GitWorktreeError(
            "implementation subtasks require a clean working tree before delegation"
        )
    return _git(root, "rev-parse", "HEAD").decode("utf-8").strip()


def repository_changed_paths(root):
    tracked = _git(root, "diff", "--name-only", "-z", "HEAD")
    untracked = _git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
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
            )
        except Exception:
            self.cleanup()
            raise
        return self.path

    def apply_patch(self, patch):
        apply_patch(self.path, patch)

    def commit_dependency_baseline(self):
        changed = self.changed_paths()
        if not changed:
            return
        _git(self.path, "add", "--", *changed)
        _git(
            self.path,
            "-c",
            "user.name=Pico",
            "-c",
            "user.email=pico@example.invalid",
            "commit",
            "-m",
            "pico dependency baseline",
        )

    def changed_paths(self):
        return repository_changed_paths(self.path)

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
            ).split(b"\0")
            if item
        }
        if untracked:
            _git(self.path, "add", "-N", "--", *sorted(untracked))
        return _git(self.path, "diff", "--binary", "--no-ext-diff", "HEAD")

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


def apply_patch(root, patch):
    payload = bytes(patch)
    _git(root, "apply", "--check", "--whitespace=nowarn", "-", input_bytes=payload)
    _git(root, "apply", "--whitespace=nowarn", "-", input_bytes=payload)
