#!/usr/bin/env python3
"""Run frozen upstream regression tests against pre-fix, reference, and Agent code."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.provenance import runtime_snapshot_id
from pico.sandbox import DockerSandbox, DockerSandboxConfig, SandboxProfile
from pico.verification import parse_verification_output
from scripts.run_real_oss_validation import (
    changed_paths,
    docker_image_id,
    file_digest,
    file_snapshot,
    git_metadata,
    require_clean_runtime,
    tree_digest,
)

DEFAULT_MANIFEST = ROOT / "validation" / "official_public_tests.json"
DEFAULT_ARTIFACT = ROOT / "artifacts" / "official-public-tests-v1.json"
DEFAULT_REPORT = ROOT / "artifacts" / "official-public-tests-v1.md"
ALLOWED_TEST_PREFIXES = ("test/", "tests/")
FULL_SHA = re.compile(r"^[a-f0-9]{40}$")
PATCH_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)

REQUIRED_TASK_FIELDS = {
    "id",
    "source_repository",
    "source_commit",
    "official_fix_commit",
    "fixture_repo",
    "agent_workspace",
    "reference_patch",
    "official_test_patch",
    "official_test_nodes",
    "pre_fix_expected",
}


def load_manifest(path=DEFAULT_MANIFEST):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "official-public-tests-v1":
        raise ValueError("unsupported official public-test manifest schema")
    if set(payload) != {
        "schema_version", "real_oss_manifest", "real_oss_suite_artifact", "tasks"
    }:
        raise ValueError("invalid official public-test manifest fields")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("official public-test manifest requires tasks")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict) or REQUIRED_TASK_FIELDS - set(task):
            raise ValueError("official public-test task is missing required fields")
        if task["id"] in ids:
            raise ValueError("official public-test task ids must be unique")
        ids.add(task["id"])
        if not FULL_SHA.fullmatch(task["source_commit"]):
            raise ValueError(f"invalid source commit for {task['id']}")
        if not FULL_SHA.fullmatch(task["official_fix_commit"]):
            raise ValueError(f"invalid official fix commit for {task['id']}")
        if not task["official_test_nodes"]:
            raise ValueError(f"official test nodes are required for {task['id']}")
        if task["pre_fix_expected"] not in {"fail", "pass"}:
            raise ValueError(f"invalid pre-fix expectation for {task['id']}")
    return payload


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


def copy_workspace(source, target, *, omit_runtime=False):
    ignore = None
    if omit_runtime:
        ignore = shutil.ignore_patterns(".pico", ".pico_hidden_verifier", "__pycache__")
    shutil.copytree(source, target, ignore=ignore)


def run_pytest(workspace, nodes, image):
    command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *nodes]
    result = DockerSandbox(
        workspace,
        DockerSandboxConfig(image=image),
    ).run(
        command,
        cwd=workspace,
        timeout=120,
        profile=SandboxProfile.VERIFY,
    )
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    details = parse_verification_output("python -m pytest -q", output, result.returncode)
    return {
        "ok": result.returncode == 0 and not result.stop_reason,
        "exit_code": result.returncode,
        "stop_reason": result.stop_reason,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "details": details,
    }


def expected_failure(result):
    output = (result["stdout"] + result["stderr"]).lower()
    return result["exit_code"] == 1 and "failed" in output


def source_snapshot_digest(root):
    payload = json.dumps(file_snapshot(root), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def suite_results_by_id(path):
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    if artifact.get("schema_version") != "real-oss-suite-evidence-v2":
        raise ValueError("unsupported Real OSS suite artifact")
    rows = {item["task"]["id"]: item for item in artifact.get("tasks", [])}
    if not rows or not all(item["result"]["passed"] for item in rows.values()):
        raise ValueError("official tests require a fully passing Real OSS suite")
    snapshots = {item.get("runtime_snapshot_id") for item in rows.values()}
    if snapshots != {runtime_snapshot_id()}:
        raise ValueError("Real OSS suite runtime snapshot is stale")
    return artifact, rows


def run_task(task, suite_item, image):
    fixture = (ROOT / task["fixture_repo"]).resolve()
    agent_workspace = (ROOT / task["agent_workspace"]).resolve()
    reference_patch = (ROOT / task["reference_patch"]).resolve()
    official_patch = (ROOT / task["official_test_patch"]).resolve()
    for path in (fixture, agent_workspace):
        if not path.is_dir():
            raise FileNotFoundError(path)
    test_paths = test_patch_paths(official_patch)
    actual_changes = changed_paths(file_snapshot(fixture), file_snapshot(agent_workspace))
    if actual_changes != suite_item["result"]["changed_files"]:
        raise ValueError(f"Agent workspace drifted for {task['id']}")

    with tempfile.TemporaryDirectory(prefix=f"pico-official-{task['id']}-") as directory:
        root = Path(directory)
        pre_fix = root / "pre_fix"
        reference = root / "reference"
        agent = root / "agent"
        copy_workspace(fixture, pre_fix)
        copy_workspace(fixture, reference)
        copy_workspace(agent_workspace, agent, omit_runtime=True)

        apply_patch(pre_fix, official_patch)
        apply_patch(reference, reference_patch)
        apply_patch(reference, official_patch)
        apply_patch(agent, official_patch)

        pre_result = run_pytest(pre_fix, task["official_test_nodes"], image)
        reference_result = run_pytest(reference, task["official_test_nodes"], image)
        agent_result = run_pytest(agent, task["official_test_nodes"], image)

    pre_failed = expected_failure(pre_result)
    baseline_matched = (
        pre_failed if task["pre_fix_expected"] == "fail" else pre_result["ok"]
    )
    passed = baseline_matched and reference_result["ok"] and agent_result["ok"]
    return {
        "task_id": task["id"],
        "source_repository": task["source_repository"],
        "source_commit": task["source_commit"],
        "official_fix_commit": task["official_fix_commit"],
        "official_test_nodes": list(task["official_test_nodes"]),
        "official_test_paths": list(test_paths),
        "fixture_tree_digest": tree_digest(fixture),
        "agent_workspace_digest": source_snapshot_digest(agent_workspace),
        "reference_patch_sha256": file_digest(reference_patch),
        "official_test_patch_sha256": file_digest(official_patch),
        "agent_changed_files": actual_changes,
        "pre_fix_failed": pre_failed,
        "pre_fix_expected": task["pre_fix_expected"],
        "pre_fix_baseline_matched": baseline_matched,
        "reference_passed": reference_result["ok"],
        "agent_passed": agent_result["ok"],
        "passed": passed,
        "results": {
            "pre_fix": pre_result,
            "reference": reference_result,
            "agent": agent_result,
        },
    }


def write_report(path, artifact):
    lines = [
        "# Official upstream public regression tests",
        "",
        f"- Overall: **{artifact['summary']['passed']}/{artifact['summary']['total']}**",
        f"- Agent assertions passed: **{artifact['summary']['agent_assertions_passed']}**",
        f"- Docker image ID: `{artifact['docker_image_id']}`",
        "",
        "| Task | Pre-fix baseline | Reference | Agent | Agent assertions |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in artifact["tasks"]:
        lines.append(
            f"| `{item['task_id']}` | "
            f"{'MATCH' if item['pre_fix_baseline_matched'] else 'MISMATCH'} "
            f"({item['pre_fix_expected']}) | "
            f"{'PASS' if item['reference_passed'] else 'FAIL'} | "
            f"{'PASS' if item['agent_passed'] else 'FAIL'} | "
            f"{item['results']['agent']['details']['passed']} |"
        )
    lines.extend([
        "",
        (
            "The test-only patches are copied from the bound official upstream fix commits. "
            "Every test run uses a no-network, read-only Docker workspace."
        ),
        "",
    ])
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--image", default="pico/official-public-tests:latest")
    args = parser.parse_args(argv)

    runtime = git_metadata()
    require_clean_runtime(runtime)
    manifest = load_manifest(args.manifest)
    real_manifest = json.loads((ROOT / manifest["real_oss_manifest"]).read_text(encoding="utf-8"))
    real_by_id = {task["id"]: task for task in real_manifest["tasks"]}
    suite_path = ROOT / manifest["real_oss_suite_artifact"]
    suite, suite_rows = suite_results_by_id(suite_path)

    tasks = []
    for task in manifest["tasks"]:
        real_task = real_by_id.get(task["id"])
        if not real_task or real_task["source_commit"] != task["source_commit"]:
            raise ValueError(f"official test task does not match Real OSS manifest: {task['id']}")
        if not task["official_fix_commit"].startswith(real_task["reference_fix_commit"]):
            raise ValueError(f"official fix commit mismatch: {task['id']}")
        row = run_task(task, suite_rows[task["id"]], args.image)
        tasks.append(row)
        print(
            f"{task['id']}: pre={'MATCH' if row['pre_fix_baseline_matched'] else 'MISMATCH'} "
            f"reference={'PASS' if row['reference_passed'] else 'FAIL'} "
            f"agent={'PASS' if row['agent_passed'] else 'FAIL'}",
            flush=True,
        )

    passed = sum(item["passed"] for item in tasks)
    artifact = {
        "schema_version": "official-public-tests-evidence-v1",
        "artifact_type": "official-public-tests",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "runtime_snapshot_id": runtime_snapshot_id(),
        "manifest_sha256": file_digest(args.manifest),
        "real_oss_suite_artifact_sha256": file_digest(suite_path),
        "real_oss_suite_runtime": suite["runtime"],
        "requirements_sha256": file_digest(
            ROOT / "validation" / "official_public_test_requirements.txt"
        ),
        "docker_image": args.image,
        "docker_image_id": docker_image_id(args.image),
        "summary": {
            "total": len(tasks),
            "passed": passed,
            "failed": len(tasks) - passed,
            "pre_fix_expected_failures": sum(item["pre_fix_failed"] for item in tasks),
            "pre_fix_baseline_matched": sum(
                item["pre_fix_baseline_matched"] for item in tasks
            ),
            "pre_fix_discriminative": sum(
                item["pre_fix_expected"] == "fail" for item in tasks
            ),
            "reference_passed": sum(item["reference_passed"] for item in tasks),
            "agent_passed": sum(item["agent_passed"] for item in tasks),
            "agent_assertions_passed": sum(
                item["results"]["agent"]["details"]["passed"] for item in tasks
            ),
        },
        "tasks": tasks,
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    write_report(args.report, artifact)
    print(f"official-public-tests: {passed}/{len(tasks)}")
    return 0 if passed == len(tasks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
