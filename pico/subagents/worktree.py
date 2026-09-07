"""Git worktrees executed through the Runtime-owned process boundary."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..execution import ExecutionContext
from ..persistence import write_once_bytes
from ..workspace import normalize_relative_file

GIT_OPERATION_TIMEOUT_SECONDS = 10
WORKTREE_SETTLEMENT_TIMEOUT_SECONDS = 10


class GitWorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitClient:
    root: Path
    command_runner_factory: object
    execution_context: ExecutionContext

    def run(self, *args, input_bytes=None):
        self.execution_context.check_active()
        root = Path(self.root).resolve()
        result = self.command_runner_factory(root).run_bytes(
            ("git", "-c", "core.hooksPath=/dev/null", *args),
            cwd=root,
            timeout=GIT_OPERATION_TIMEOUT_SECONDS,
            env={},
            execution_context=self.execution_context,
            input_bytes=input_bytes,
        )
        if result.stop_reason:
            self.execution_context.check_active()
            raise GitWorktreeError("Git command reached its operation timeout")
        if result.infrastructure_error or result.returncode:
            detail = (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            )
            raise GitWorktreeError(
                detail.strip() or f"git {' '.join(args)} failed"
            )
        return result.stdout


def repository_changed_paths(client):
    if not isinstance(client, GitClient):
        raise TypeError("repository observation requires GitClient")
    tracked = client.run("diff", "--name-only", "-z", "HEAD")
    untracked = client.run(
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
    command_runner_factory: object
    execution_context: ExecutionContext
    planned_path: Path | None = None
    path: Path | None = None
    container_root: Path | None = None

    def _client(self, root=None, *, execution_context=None):
        return GitClient(
            Path(root or self.repository_root),
            self.command_runner_factory,
            execution_context or self.execution_context,
        )

    def create(self):
        if self.path is not None:
            raise RuntimeError("Git worktree is already attached")
        if self.planned_path is None:
            self.container_root = Path(tempfile.mkdtemp(prefix="pico-subagent-"))
            target = self.container_root / self.label
        else:
            target = Path(self.planned_path).resolve()
            self.container_root = target.parent
            self.container_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise GitWorktreeError(f"planned worktree already exists: {target}")
        try:
            self._client().run(
                "worktree",
                "add",
                "--detach",
                str(target),
                self.base_sha,
            )
        except Exception:
            self.path = target
            self.cleanup()
            raise
        self.path = target
        return self.path

    def changed_paths(self):
        if self.path is None:
            raise GitWorktreeError("worktree is not attached")
        return repository_changed_paths(self._client(self.path))

    def patch(self, changed_paths):
        if self.path is None:
            raise GitWorktreeError("worktree is not attached")
        client = self._client(self.path)
        existing = [path for path in changed_paths if (self.path / path).is_file()]
        if existing:
            client.run("add", "-N", "--force", "--", *existing)
        return client.run("diff", "--binary", "--no-ext-diff", "HEAD")

    def write_patch(self, destination, changed_paths):
        payload = self.patch(changed_paths)
        if self.changed_paths() != tuple(changed_paths):
            raise GitWorktreeError("delivery patch paths do not match the Run changes")
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
        target = self.path or (
            Path(self.planned_path).resolve()
            if self.planned_path is not None
            else None
        )
        cleanup_context = ExecutionContext.standalone(
            max_seconds=WORKTREE_SETTLEMENT_TIMEOUT_SECONDS
        )
        if target is not None and target.exists():
            try:
                self._client(execution_context=cleanup_context).run(
                    "worktree", "remove", "--force", str(target)
                )
            except (GitWorktreeError, OSError):
                shutil.rmtree(target, ignore_errors=True)
        try:
            self._client(execution_context=cleanup_context).run(
                "worktree", "prune"
            )
        except (GitWorktreeError, OSError):
            pass
        if self.planned_path is None and self.container_root is not None:
            shutil.rmtree(self.container_root, ignore_errors=True)
        elif self.container_root is not None:
            try:
                self.container_root.rmdir()
            except OSError:
                pass
        self.path = None
        self.container_root = None
