"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的“仓库第一印象”。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import subprocess
import textwrap
from pathlib import Path, PurePosixPath

# 这些文件最可能直接影响 agent 的行动方式。
# 我们不会预加载整个仓库，只会先给模型一小份“导航包”。
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
    def __init__(self, cwd, repo_root, branch, default_branch, git_status, recent_commits, project_docs):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.git_status = git_status
        self.recent_commits = recent_commits
        self.project_docs = project_docs

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
        docs = {}
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
                if key in docs:
                    continue
                docs[key] = clip(
                    resolved.read_text(encoding="utf-8", errors="replace"),
                    1200,
                )

        default_branch = git(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main"
        ).removeprefix("origin/") or "main"
        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=default_branch,
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
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
        )

    def text(self):
        # 这段文本会被塞进 prompt prefix，作为相对稳定的基线上下文。
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        try:
            logical_cwd = Path(self.cwd).relative_to(Path(self.repo_root)).as_posix()
        except ValueError:
            logical_cwd = "."
        logical_cwd = logical_cwd or "."
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {logical_cwd}
            - repo_root: .
            - shell_cwd: /workspace
            - branch: {self.branch}
            - default_branch: {self.default_branch}
            - status:
            {self.git_status}
            - recent_commits:
            {commits}
            - project_docs:
            {docs}
            """
        ).strip()

    def state(self):
        return {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "git_status": self.git_status,
            "recent_commits": list(self.recent_commits),
            "project_docs": dict(self.project_docs),
        }
