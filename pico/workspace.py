"""Workspace path boundary and one explicit model-facing observation."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .mutations import file_revision

IGNORED_PATH_NAMES = {".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}
TOOL_INTERNAL_PATH_NAMES = frozenset({".git", ".pico"})
WORKSPACE_GIT_TIMEOUT_SECONDS = 5
WORKSPACE_STATUS_MAX_CHARS = 1500


def normalize_relative_file(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or text == "." or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be a repository-relative file")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError("path must be normalized")
    return path.as_posix()


def clip(text, limit):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


def _discover_repository(cwd):
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=WORKSPACE_GIT_TIMEOUT_SECONDS,
        )
    except OSError:
        return Path(cwd).resolve(), "unavailable"
    except subprocess.SubprocessError:
        return Path(cwd).resolve(), "unavailable"
    root = result.stdout.strip()
    if result.returncode == 0 and root:
        return Path(root).resolve(), "git"
    return Path(cwd).resolve(), "filesystem"


@dataclass(frozen=True)
class WorkspaceObservation:
    repository: str
    head: str
    status: str
    status_lines: tuple[str, ...] = ()
    truncated: bool = False

    def render(self, *, root, logical_cwd):
        lines = [
            "Workspace:",
            f"- Root: {root}",
            f"- startup_directory (relative to workspace root): {logical_cwd}",
            "- Relative tool paths use Root; '.' means Root.",
        ]
        if self.repository == "filesystem":
            lines.append("- Git: not a Git repository.")
            return "\n".join(lines)
        if self.repository == "unavailable":
            lines.append("- Git: repository detection unavailable; state unknown.")
            return "\n".join(lines)
        state = "status unavailable; do not assume clean" if self.status == "unavailable" else self.status
        lines.append(f"- Git snapshot (at context build): {self.head}; {state}.")
        if any(
            line[:2] in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
            for line in self.status_lines
        ):
            lines.append("- Merge conflicts are present in the listed paths.")
        if self.status_lines:
            lines.append("Existing changes (Git short status):")
            lines.extend(f"  {line}" for line in self.status_lines)
        if self.truncated:
            lines.append("- Change list truncated; not all paths are shown.")
        return "\n".join(lines)


class Workspace:
    def __init__(self, cwd, root, repository):
        self.cwd = Path(cwd).resolve()
        self.root = Path(root).resolve()
        if repository not in {"git", "filesystem", "unavailable"}:
            raise ValueError("invalid workspace repository kind")
        self.repository = repository

    @classmethod
    def build(cls, cwd, repo_root_override=None):
        cwd = Path(cwd).resolve()
        discovered_root, repository = _discover_repository(
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else cwd
        )
        root = (
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else discovered_root
        )
        return cls(cwd, root, repository)

    @staticmethod
    def _git(command_runner, execution_context, root, args):
        result = command_runner.run_bytes(
            ("git", "--no-optional-locks", *args),
            cwd=root,
            timeout=WORKSPACE_GIT_TIMEOUT_SECONDS,
            env={},
            execution_context=execution_context,
        )
        if result.stop_reason:
            execution_context.check_active()
        return result

    @staticmethod
    def _stdout(result):
        return result.stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _head_observation(command_runner, execution_context, root):
        symbolic = Workspace._git(
            command_runner,
            execution_context,
            root,
            ("symbolic-ref", "--quiet", "--short", "HEAD"),
        )
        commit = Workspace._git(
            command_runner,
            execution_context,
            root,
            ("rev-parse", "--verify", "HEAD"),
        )
        symbolic_stdout = Workspace._stdout(symbolic)
        commit_stdout = Workspace._stdout(commit)
        if symbolic.returncode == 0 and symbolic_stdout:
            branch = symbolic_stdout
            return (
                f"branch {branch}"
                if commit.returncode == 0
                else f"unborn {branch}"
            )
        if commit.returncode == 0 and commit_stdout:
            return "detached " + commit_stdout[:12]
        return "unavailable"

    def observe(self, *, command_runner, execution_context):
        execution_context.check_active()
        if self.repository != "git":
            return WorkspaceObservation(
                repository=self.repository,
                head="not_applicable",
                status=(
                    "not_applicable"
                    if self.repository == "filesystem"
                    else "unavailable"
                ),
            )
        head = self._head_observation(
            command_runner, execution_context, self.root
        )
        pathspec = (
            "--",
            ".",
            ":(exclude,top).pico",
            ":(exclude,top).pico/**",
        )
        status = self._git(
            command_runner,
            execution_context,
            self.root,
            ("status", "--short", *pathspec),
        )
        if status.infrastructure_error or status.returncode != 0:
            return WorkspaceObservation(
                repository="git",
                head=head,
                status="unavailable",
            )
        raw_status = status.stdout.decode("utf-8", errors="replace").rstrip()
        all_status_lines = tuple(raw_status.splitlines())
        visible_lines = []
        visible_chars = 0
        for line in all_status_lines:
            cost = len(line) + (1 if visible_lines else 0)
            if visible_chars + cost > WORKSPACE_STATUS_MAX_CHARS:
                break
            visible_lines.append(line)
            visible_chars += cost
        status_lines = tuple(visible_lines)
        truncated = len(status_lines) < len(all_status_lines)
        return WorkspaceObservation(
            repository="git",
            head=head,
            status="dirty" if raw_status else "clean",
            status_lines=status_lines,
            truncated=truncated,
        )

    def text(self, *, command_runner, execution_context):
        try:
            logical_cwd = self.cwd.relative_to(self.root).as_posix()
        except ValueError:
            logical_cwd = "."
        logical_cwd = logical_cwd or "."
        return self.observe(
            command_runner=command_runner,
            execution_context=execution_context,
        ).render(root=self.root, logical_cwd=logical_cwd)

    @staticmethod
    def path_state(path, *, execution_context) -> str:
        path = Path(path)
        execution_context.check_active()
        try:
            if path.is_file():
                return file_revision(
                    path,
                    execution_context=execution_context,
                )
            if path.is_dir():
                metadata = path.stat()
                return f"dir:{metadata.st_mtime_ns}:{metadata.st_ctime_ns}"
        except OSError:
            return "unavailable"
        return "absent"

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
