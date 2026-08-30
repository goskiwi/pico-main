"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的“仓库第一印象”。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import subprocess
from pathlib import Path, PurePosixPath

# 普通项目文件只暴露名称；AGENTS.md 正文单独作为 repository conventions。
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
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
        document_names,
        repository_conventions,
    ):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.git_status = git_status
        self.document_names = tuple(document_names)
        self.repository_conventions = dict(repository_conventions)

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
        document_names = []
        repository_conventions = {}
        # 同时扫描 repo_root 和 cwd，这样在子目录启动时也能看到本地文档；
        # 但用相对路径做 key，避免同一份文档被重复收集。
        for base in (repo_root, cwd):
            for name in DOC_NAMES:
                path = base / name
                if not path.is_file() or path.is_symlink():
                    continue
                resolved = path.resolve()
                try:
                    key = resolved.relative_to(repo_root).as_posix()
                except ValueError:
                    continue
                if key in document_names:
                    continue
                document_names.append(key)
                if name == "AGENTS.md":
                    repository_conventions[key] = clip(
                        resolved.read_text(encoding="utf-8", errors="replace"),
                        1200,
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
            document_names=document_names,
            repository_conventions=repository_conventions,
        )

    def text(self):
        try:
            logical_cwd = Path(self.cwd).relative_to(Path(self.repo_root)).as_posix()
        except ValueError:
            logical_cwd = "."
        logical_cwd = logical_cwd or "."
        status = str(self.git_status or "clean").splitlines() or ["clean"]
        documents = list(self.document_names) or ["none"]
        lines = [
            "Workspace:",
            f"- cwd: {logical_cwd}",
            f"- branch: {self.branch}",
            "- status:",
            *(f"  {line}" for line in status),
            "- document_names:",
            *(f"  - {name}" for name in documents),
        ]
        return "\n".join(lines)

    def state(self):
        return {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "git_status": self.git_status,
            "document_names": list(self.document_names),
            "repository_conventions": dict(self.repository_conventions),
        }
