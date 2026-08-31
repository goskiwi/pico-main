"""Shared helpers for current real Triage and compaction evidence runners."""

from __future__ import annotations

import fnmatch
import hashlib
import shutil
import subprocess
from pathlib import Path

from pico.command_runner import CommandRunner, shell_argv

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_CHANGE_GLOBS = (
    "tests/**",
    "test/**",
    "testing/**",
    ".pico_hidden_verifier/**",
)


def file_snapshot(root):
    root = Path(root)
    snapshot = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if relative.parts[0] in {".git", ".pico", ".pico_hidden_verifier"}:
            continue
        if "__pycache__" in relative.parts:
            continue
        snapshot[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def changed_paths(before, after):
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


def matches(path, patterns):
    return any(
        fnmatch.fnmatch(path, pattern)
        or ("/**/" in pattern and fnmatch.fnmatch(path, pattern.replace("/**/", "/")))
        for pattern in patterns
    )


def run_verifier(root, task):
    source = (ROOT / task["verifier_file"]).resolve()
    target = (Path(root) / ".pico_hidden_verifier" / "test_hidden.py").resolve()
    target.relative_to(Path(root).resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    result = CommandRunner(root).run(
        shell_argv(task["verifier_command"]), cwd=root, timeout=90, env={}
    )
    return {
        "ok": result.returncode == 0 and not result.stop_reason,
        "infrastructure_error": result.infrastructure_error,
        "exit_code": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "stop_reason": result.stop_reason,
    }


def git_metadata():
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "branch": git("branch", "--show-current"),
        "commit_sha": git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(git("status", "--porcelain")),
    }


def require_clean_runtime(metadata):
    if metadata["working_tree_dirty"]:
        raise RuntimeError(
            "Real evaluation requires a clean worktree; commit the Runtime first"
        )
