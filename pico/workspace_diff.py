"""Workspace change detection for risky tool execution."""

import hashlib
import subprocess

from .config import IGNORED_PATH_NAMES


def capture_workspace_snapshot(agent):
    snapshot = {}
    for path in agent.root.rglob("*"):
        try:
            relative_parts = path.relative_to(agent.root).parts
        except ValueError:
            continue
        if any(part in IGNORED_PATH_NAMES for part in relative_parts):
            continue
        if not path.is_file():
            continue
        try:
            snapshot[path.relative_to(agent.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            continue
    return snapshot


def capture_path_snapshot(agent, paths):
    snapshot = {}
    for raw_path in paths:
        if not str(raw_path or "").strip():
            continue
        try:
            path = agent.path(raw_path)
            relative_path = path.relative_to(agent.root).as_posix()
            relative_parts = path.relative_to(agent.root).parts
        except Exception:
            continue
        if any(part in IGNORED_PATH_NAMES for part in relative_parts):
            continue
        if not path.exists():
            snapshot[relative_path] = None
            continue
        if not path.is_file():
            continue
        try:
            snapshot[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            continue
    return snapshot


def capture_git_status_snapshot(agent):
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=agent.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except Exception:
        return None

    snapshot = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        status = line[:2]
        path_text = line[3:] if len(line) > 3 else ""
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        path_text = path_text.strip().strip('"')
        if not path_text:
            continue
        parts = tuple(part for part in path_text.split("/") if part)
        if any(part in IGNORED_PATH_NAMES for part in parts):
            continue
        snapshot[path_text] = status
    return snapshot


def diff_workspace_snapshots(before, after):
    changed_paths = []
    summaries = []
    all_paths = sorted(set(before) | set(after))
    for path in all_paths:
        if before.get(path) == after.get(path):
            continue
        changed_paths.append(path)
        if before.get(path) is None and after.get(path) is not None:
            summaries.append(f"created:{path}")
        elif before.get(path) is not None and after.get(path) is None:
            summaries.append(f"deleted:{path}")
        elif path not in before:
            after_status = str(after.get(path, ""))
            if "D" in after_status:
                summaries.append(f"deleted:{path}")
            elif after_status.strip() == "??" or "A" in after_status:
                summaries.append(f"created:{path}")
            else:
                summaries.append(f"modified:{path}")
        elif path not in after:
            before_status = str(before.get(path, ""))
            if "D" in before_status:
                summaries.append(f"modified:{path}")
            else:
                summaries.append(f"deleted:{path}")
        else:
            summaries.append(f"modified:{path}")
    return changed_paths, summaries


def target_snapshot_paths(name, args):
    if name in {"write_file", "patch_file"}:
        return [str((args or {}).get("path", ""))]
    return []


def before_workspace_snapshot(agent, name, args, tool):
    if not tool["risky"]:
        return {}, "none"
    paths = target_snapshot_paths(name, args)
    if paths:
        return capture_path_snapshot(agent, paths), "paths"
    if name == "run_shell":
        git_snapshot = capture_git_status_snapshot(agent)
        if git_snapshot is not None:
            return git_snapshot, "git_status"
    return capture_workspace_snapshot(agent), "full"


def after_workspace_snapshot(agent, name, args, tool, mode, before_snapshot):
    if not tool["risky"]:
        return before_snapshot
    if mode == "paths":
        return capture_path_snapshot(agent, target_snapshot_paths(name, args))
    if mode == "git_status":
        git_snapshot = capture_git_status_snapshot(agent)
        if git_snapshot is not None:
            return git_snapshot
    if mode == "none":
        return before_snapshot
    return capture_workspace_snapshot(agent)
