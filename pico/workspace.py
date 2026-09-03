"""工作区路径边界与按需 Git 视图。

Workspace 只保存稳定路径；branch/status 在新 Provider Prompt 构建时现场读取。
"""

import os
import subprocess
from pathlib import Path, PurePosixPath

from .mutations import file_revision

IGNORED_PATH_NAMES = {".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}
TOOL_INTERNAL_PATH_NAMES = frozenset({".git", ".pico"})


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


def _git(cwd, args, fallback=""):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except (OSError, subprocess.SubprocessError):
        return fallback


class Workspace:
    def __init__(self, cwd, root):
        self.cwd = Path(cwd).resolve()
        self.root = Path(root).resolve()

    @classmethod
    def build(cls, cwd, repo_root_override=None):
        cwd = Path(cwd).resolve()
        repo_root = (
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else Path(
                _git(cwd, ["rev-parse", "--show-toplevel"], str(cwd))
            ).resolve()
        )
        return cls(cwd, repo_root)

    @property
    def branch(self):
        return _git(self.cwd, ["branch", "--show-current"], "-") or "-"

    def text(self):
        try:
            logical_cwd = self.cwd.relative_to(self.root).as_posix()
        except ValueError:
            logical_cwd = "."
        logical_cwd = logical_cwd or "."
        status = clip(
            _git(
                self.cwd,
                [
                    "status",
                    "--short",
                    "--",
                    ":(exclude,top).pico",
                    ":(exclude,top).pico/**",
                ],
                "clean",
            )
            or "clean",
            1500,
        ).splitlines()
        lines = [
            "Workspace:",
            f"- cwd: {logical_cwd}",
            f"- branch: {self.branch}",
            "- status:",
            *(f"  {line}" for line in status),
        ]
        return "\n".join(lines)

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
