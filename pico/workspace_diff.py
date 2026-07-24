"""Workspace change detection for risky tool execution."""

import hashlib
from dataclasses import dataclass

from .config import IGNORED_PATH_NAMES


@dataclass
class SnapshotCacheEntry:
    mtime_ns: int
    size: int
    digest: str


def _snapshot_cache(agent):
    return agent._workspace_snapshot_hash_cache


def _ignored_relative_parts(relative_parts):
    return any(part in IGNORED_PATH_NAMES for part in relative_parts)


def _hash_file(agent, path, relative_path, stat_result=None):
    try:
        stat_result = stat_result or path.stat()
    except OSError:
        return None

    cache = _snapshot_cache(agent)
    cached = cache.get(relative_path)
    if (
        isinstance(cached, SnapshotCacheEntry)
        and cached.mtime_ns == stat_result.st_mtime_ns
        and cached.size == stat_result.st_size
    ):
        return cached.digest

    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        cache.pop(relative_path, None)
        return None

    cache[relative_path] = SnapshotCacheEntry(
        mtime_ns=stat_result.st_mtime_ns,
        size=stat_result.st_size,
        digest=digest,
    )
    return digest


def capture_workspace_snapshot(agent):
    snapshot = {}
    seen_paths = set()
    for path in agent.root.rglob("*"):
        relative_parts = path.relative_to(agent.root).parts
        if _ignored_relative_parts(relative_parts):
            continue
        if not path.is_file():
            continue
        relative_path = path.relative_to(agent.root).as_posix()
        try:
            stat_result = path.stat()
        except OSError:
            continue
        digest = _hash_file(agent, path, relative_path, stat_result=stat_result)
        if digest is None:
            continue
        seen_paths.add(relative_path)
        snapshot[relative_path] = digest
    cache = _snapshot_cache(agent)
    for relative_path in list(cache):
        if relative_path not in seen_paths:
            cache.pop(relative_path, None)
    return snapshot


def capture_path_snapshot(agent, paths):
    snapshot = {}
    for raw_path in paths:
        if not str(raw_path or "").strip():
            continue
        path = agent.path(raw_path)
        relative_path = path.relative_to(agent.root).as_posix()
        relative_parts = path.relative_to(agent.root).parts
        if _ignored_relative_parts(relative_parts):
            continue
        if not path.exists():
            _snapshot_cache(agent).pop(relative_path, None)
            snapshot[relative_path] = None
            continue
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        digest = _hash_file(agent, path, relative_path, stat_result=stat_result)
        if digest is not None:
            snapshot[relative_path] = digest
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
            summaries.append(f"created:{path}")
        elif path not in after:
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
    return capture_workspace_snapshot(agent), "full"


def after_workspace_snapshot(agent, name, args, tool, mode, before_snapshot):
    if not tool["risky"]:
        return before_snapshot
    if mode == "paths":
        return capture_path_snapshot(agent, target_snapshot_paths(name, args))
    if mode == "none":
        return before_snapshot
    return capture_workspace_snapshot(agent)
