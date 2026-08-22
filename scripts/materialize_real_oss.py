#!/usr/bin/env python3
"""Materialize exact pre-fix checkouts for the frozen Real OSS suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "validation" / "real_oss_suite.json"
FIXTURE_ROOT = (ROOT / "artifacts" / "real-oss-fixtures").resolve()
FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
REQUIRED_TASK_FIELDS = {
    "id",
    "prompt",
    "fixture_repo",
    "required_change_globs",
    "allowed_change_globs",
    "verifier_file",
    "verifier_command",
    "source_repository",
    "source_commit",
    "reference_fix_commit",
    "reference_patch",
    "expected_files",
}


def run_git(args, cwd=None):
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def tree_digest(root):
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def load_manifest(path=DEFAULT_MANIFEST):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "real-oss-suite-v3":
        raise ValueError("unsupported Real OSS suite schema")
    if int(payload.get("tool_budget", 0)) < 1:
        raise ValueError("Real OSS suite requires one positive uniform tool_budget")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Real OSS suite requires a non-empty task list")
    ids = [str(task.get("id", "")) for task in tasks]
    if any(not task_id for task_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Real OSS task ids must be unique and non-empty")
    for task in tasks:
        if not isinstance(task, dict) or REQUIRED_TASK_FIELDS - set(task):
            raise ValueError("Real OSS task is missing required fields")
        for field in ("source_commit", "reference_fix_commit"):
            if not FULL_SHA.fullmatch(str(task[field])):
                raise ValueError(f"Real OSS task {field} must be a full commit SHA")
    return payload


def select_tasks(manifest, task_ids=()):
    selected_ids = set(task_ids)
    tasks = list(manifest["tasks"])
    if not selected_ids:
        return tasks
    unknown = selected_ids - {task["id"] for task in tasks}
    if unknown:
        raise ValueError(f"unknown Real OSS task: {', '.join(sorted(unknown))}")
    return [task for task in tasks if task["id"] in selected_ids]


def materialize_task(task, *, replace=False):
    target = (ROOT / task["fixture_repo"]).resolve()
    target.relative_to(FIXTURE_ROOT)
    if target.exists():
        if not replace:
            raise FileExistsError(f"fixture exists: {target}; use --replace")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--quiet", str(target)])
    run_git(
        ["fetch", "--quiet", "--depth", "1", task["source_repository"], task["source_commit"]],
        cwd=target,
    )
    run_git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=target)
    actual_commit = run_git(["rev-parse", "HEAD"], cwd=target)
    if actual_commit != task["source_commit"]:
        raise RuntimeError(f"resolved {actual_commit}, expected {task['source_commit']}")
    for relative in task["expected_files"]:
        if not (target / relative).is_file():
            raise RuntimeError(f"fixture missing expected file: {relative}")
    shutil.rmtree(target / ".git")
    for relative, content in dict(task.get("generated_files") or {}).items():
        generated = (target / relative).resolve()
        generated.relative_to(target)
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text(str(content), encoding="utf-8")
    return {
        "task_id": task["id"],
        "source_repository": task["source_repository"],
        "source_commit": actual_commit,
        "tree_digest": tree_digest(target),
    }


def materialize(manifest_path=DEFAULT_MANIFEST, *, task_ids=(), replace=False):
    manifest = load_manifest(manifest_path)
    records = [
        materialize_task(task, replace=replace)
        for task in select_tasks(manifest, task_ids)
    ]
    sidecar = FIXTURE_ROOT / ".real_oss_suite.materialization.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(
            {"schema_version": "real-oss-materialization-v1", "tasks": records},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return records


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(materialize(args.manifest, task_ids=args.task, replace=args.replace), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
