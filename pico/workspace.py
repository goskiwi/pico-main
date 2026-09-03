"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的“仓库第一印象”。
这份快照刻意保持小而稳定：只包含调用路径和 Git 事实。
"""

import subprocess
from pathlib import Path, PurePosixPath

IGNORED_PATH_NAMES = {".git", ".pico", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


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


class WorkspaceContext:
    def __init__(
        self,
        cwd,
        repo_root,
        branch,
        git_status,
    ):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.git_status = git_status

    @classmethod
    def build(cls, cwd, repo_root_override=None):
        cwd = Path(cwd).resolve()

        def git(args, fallback=""):
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

        repo_root = (
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else Path(git(["rev-parse", "--show-toplevel"], str(cwd))).resolve()
        )
        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",
            git_status=clip(
                git(
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
            ),
        )

    def text(self):
        try:
            logical_cwd = Path(self.cwd).relative_to(Path(self.repo_root)).as_posix()
        except ValueError:
            logical_cwd = "."
        logical_cwd = logical_cwd or "."
        status = str(self.git_status or "clean").splitlines() or ["clean"]
        lines = [
            "Workspace:",
            f"- cwd: {logical_cwd}",
            f"- branch: {self.branch}",
            "- status:",
            *(f"  {line}" for line in status),
        ]
        return "\n".join(lines)

    def state(self):
        return {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "git_status": self.git_status,
        }
