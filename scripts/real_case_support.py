"""Small Git helpers for the real Compaction evidence runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
