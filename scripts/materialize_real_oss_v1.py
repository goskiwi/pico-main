#!/usr/bin/env python3
"""Materialize the frozen upstream source checkouts used by Real OSS V1.

The Agent receives only the pre-fix source tree. Git metadata is removed after
the exact commit is verified so historical fix commits cannot leak through
``git log`` or ``git show``. Provenance is retained in a sidecar outside every
Agent workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "benchmarks" / "real_oss_v1.json"
MATERIALIZATION_ROOT = Path("artifacts/real-oss-fixtures")
REQUIRED_SOURCE_FIELDS = (
    "id",
    "fixture_repo",
    "source_repository",
    "source_commit",
    "expected_files",
)


def _run_git(args: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed" + (f": {detail}" if detail else ""))
    return completed.stdout.strip()


def _inside(root: Path, value: str | Path, *, label: str) -> Path:
    root = Path(root).resolve()
    target = (root / Path(value)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes {root}: {target}") from exc
    return target


def _fixture_path(repo_root: Path, task: dict[str, Any]) -> Path:
    relative = Path(str(task["fixture_repo"]))
    if relative.parts[:2] != MATERIALIZATION_ROOT.parts:
        raise ValueError(
            f"task {task['id']} fixture_repo must be below {MATERIALIZATION_ROOT}"
        )
    return _inside(repo_root, relative, label=f"task {task['id']} fixture_repo")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=str):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Real OSS manifest must have schema_version 1")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Real OSS manifest must contain at least one task")
    seen_ids = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("Real OSS task must be an object")
        missing = [key for key in REQUIRED_SOURCE_FIELDS if key not in task]
        if missing:
            raise ValueError(
                f"Real OSS task {task.get('id', '<unknown>')} missing: {', '.join(missing)}"
            )
        task_id = str(task["id"]).strip()
        if not task_id or task_id in seen_ids:
            raise ValueError(f"Real OSS task has duplicate or empty id: {task_id!r}")
        seen_ids.add(task_id)
        commit = str(task["source_commit"]).strip()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise ValueError(f"task {task_id} source_commit must be a lowercase SHA-1")
        repository = str(task["source_repository"]).strip()
        if not repository.startswith("https://") or not repository.endswith(".git"):
            raise ValueError(f"task {task_id} source_repository must be an HTTPS Git URL")
        expected_files = task["expected_files"]
        if not isinstance(expected_files, list) or not expected_files:
            raise ValueError(f"task {task_id} expected_files must be a non-empty list")
        if any(not str(item).strip() or Path(str(item)).is_absolute() for item in expected_files):
            raise ValueError(f"task {task_id} expected_files must be non-empty relative paths")
        generated_files = task.get("generated_files", {})
        if not isinstance(generated_files, dict):
            raise ValueError(f"task {task_id} generated_files must be an object")
        for relative, content in generated_files.items():
            if not str(relative).strip() or Path(str(relative)).is_absolute():
                raise ValueError(f"task {task_id} generated file path must be relative")
            if not isinstance(content, str):
                raise ValueError(f"task {task_id} generated file content must be text")
    return payload


def materialize_task(repo_root: Path, task: dict[str, Any], *, replace: bool) -> dict[str, Any]:
    target = _fixture_path(repo_root, task)
    if target.exists():
        if not replace:
            raise FileExistsError(f"fixture already exists: {target}; rerun with --replace")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    _run_git(["init", "--quiet", str(target)])
    _run_git(["fetch", "--quiet", "--depth", "1", task["source_repository"], task["source_commit"]], cwd=target)
    _run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=target)

    actual_commit = _run_git(["rev-parse", "HEAD"], cwd=target)
    if actual_commit != task["source_commit"]:
        raise RuntimeError(
            f"task {task['id']} resolved {actual_commit}, expected {task['source_commit']}"
        )
    commit_count = int(_run_git(["rev-list", "--all", "--count"], cwd=target))
    if commit_count != 1:
        raise RuntimeError(f"task {task['id']} fetched {commit_count} commits, expected exactly one")
    if _run_git(["status", "--porcelain"], cwd=target):
        raise RuntimeError(f"task {task['id']} fixture is dirty after checkout")
    for relative in task["expected_files"]:
        if not (target / relative).is_file():
            raise RuntimeError(f"task {task['id']} missing expected file: {relative}")
    if (target / ".benchmark_hidden").exists():
        raise RuntimeError(f"task {task['id']} checkout unexpectedly contains .benchmark_hidden")

    shutil.rmtree(target / ".git")
    for relative, content in dict(task.get("generated_files") or {}).items():
        generated = _inside(target, relative, label=f"task {task['id']} generated file")
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(content, encoding="utf-8")
    return {
        "id": task["id"],
        "fixture_repo": str(Path(task["fixture_repo"])),
        "source_repository": task["source_repository"],
        "source_commit": actual_commit,
        "tree_digest": _tree_digest(target),
    }


def materialize_manifest(
    manifest_path: Path,
    *,
    repo_root: Path = ROOT,
    task_ids: tuple[str, ...] = (),
    replace: bool = False,
) -> list[dict[str, Any]]:
    repo_root = Path(repo_root).resolve()
    manifest = load_manifest(manifest_path)
    requested = {str(task_id) for task_id in task_ids}
    tasks = [task for task in manifest["tasks"] if not requested or task["id"] in requested]
    missing = requested - {task["id"] for task in tasks}
    if missing:
        raise ValueError(f"unknown Real OSS task ids: {', '.join(sorted(missing))}")
    records = [materialize_task(repo_root, task, replace=replace) for task in tasks]
    manifest_label = Path(manifest_path).stem
    sidecar = (
        _inside(repo_root, MATERIALIZATION_ROOT, label="materialization root")
        / f".{manifest_label}.materialization.json"
    )
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps({"manifest": str(Path(manifest_path).resolve()), "tasks": records}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return records


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize frozen Real OSS V1 fixtures from exact upstream commits."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--task", action="append", dest="task_ids")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing fixture only after resolving its exact target path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    records = materialize_manifest(
        args.manifest,
        task_ids=tuple(args.task_ids or ()),
        replace=bool(args.replace),
    )
    for record in records:
        print(f"{record['id']}: {record['source_commit']} {record['tree_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
