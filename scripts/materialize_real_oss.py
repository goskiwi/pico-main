#!/usr/bin/env python3
"""Materialize the exact pre-fix Click checkout used by Real OSS validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "validation" / "click_real_oss.json"


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


def load_task(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "click-real-oss-validation-v1":
        raise ValueError("unsupported Real OSS validation schema")
    return payload["task"]


def materialize(manifest_path=DEFAULT_MANIFEST, *, replace=False):
    task = load_task(manifest_path)
    target = (ROOT / task["fixture_repo"]).resolve()
    fixture_root = (ROOT / "artifacts" / "real-oss-fixtures").resolve()
    target.relative_to(fixture_root)
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
    record = {
        "task_id": task["id"],
        "source_repository": task["source_repository"],
        "source_commit": actual_commit,
        "tree_digest": tree_digest(target),
    }
    sidecar = target.parent / ".click_real_oss.materialization.json"
    sidecar.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(materialize(args.manifest, replace=args.replace), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
