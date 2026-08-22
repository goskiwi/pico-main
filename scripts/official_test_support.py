"""Frozen official-test manifest and patch helpers used by real Triage."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from scripts.materialize_real_oss import load_manifest as load_real_manifest

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "validation" / "official_public_tests.json"
ALLOWED_TEST_PREFIXES = ("test/", "tests/")
PATCH_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)
REQUIRED_TASK_FIELDS = {
    "id",
    "official_test_patch",
    "official_test_nodes",
    "pre_fix_expected",
}


def load_manifest(path=DEFAULT_MANIFEST):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "official-public-tests-v2":
        raise ValueError("unsupported official public-test manifest schema")
    if set(payload) != {"schema_version", "real_oss_manifest", "tasks"}:
        raise ValueError("invalid official public-test manifest fields")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("official public-test manifest requires tasks")
    real_manifest = load_real_manifest(ROOT / payload["real_oss_manifest"])
    real_by_id = {task["id"]: task for task in real_manifest["tasks"]}
    ids = set()
    merged_tasks = []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != REQUIRED_TASK_FIELDS:
            raise ValueError("official public-test task has invalid fields")
        if task["id"] in ids:
            raise ValueError("official public-test task ids must be unique")
        ids.add(task["id"])
        real_task = real_by_id.get(task["id"])
        if real_task is None:
            raise ValueError(f"unknown Real OSS task: {task['id']}")
        if not task["official_test_nodes"]:
            raise ValueError(f"official test nodes are required for {task['id']}")
        if task["pre_fix_expected"] not in {"fail", "pass"}:
            raise ValueError(f"invalid pre-fix expectation for {task['id']}")
        merged_tasks.append(
            {
                **real_task,
                **task,
                "official_fix_commit": real_task["reference_fix_commit"],
            }
        )
    return {**payload, "tasks": merged_tasks}


def test_patch_paths(path):
    text = Path(path).read_text(encoding="utf-8")
    pairs = PATCH_HEADER.findall(text)
    if not pairs:
        raise ValueError(f"official test patch has no file changes: {path}")
    paths = []
    for before, after in pairs:
        if before != after:
            raise ValueError("official test patch cannot rename files")
        if not before.startswith(ALLOWED_TEST_PREFIXES):
            raise ValueError(f"official test patch changes non-test path: {before}")
        paths.append(before)
    return tuple(paths)


def apply_patch(workspace, patch_path):
    result = subprocess.run(
        [
            "patch", "-p1", "--batch", "--forward", "-i",
            str(Path(patch_path).resolve()),
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"patch failed in {workspace}: {(result.stderr or result.stdout).strip()}"
        )


def expected_failure(result):
    output = (result["stdout"] + result["stderr"]).lower()
    return result["exit_code"] == 1 and "failed" in output
