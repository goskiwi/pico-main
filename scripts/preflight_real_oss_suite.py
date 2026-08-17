#!/usr/bin/env python3
"""Verify every hidden test fails against its frozen pre-fix checkout."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.materialize_real_oss import DEFAULT_MANIFEST, load_manifest
from scripts.run_real_oss_validation import (
    docker_image_id,
    file_digest,
    git_metadata,
    require_clean_runtime,
    run_verifier,
    tree_digest,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--sandbox-image", default="pico/real-oss-suite:latest")
    args = parser.parse_args(argv)
    runtime = git_metadata()
    require_clean_runtime(runtime)
    rows = []
    for task in load_manifest(args.manifest)["tasks"]:
        fixture = (ROOT / task["fixture_repo"]).resolve()
        with tempfile.TemporaryDirectory(prefix=f"pico-preflight-{task['id']}-") as directory:
            workspace = Path(directory) / task["id"]
            shutil.copytree(fixture, workspace)
            before = run_verifier(workspace, task, args.sandbox_image)
            patch_path = (ROOT / task["reference_patch"]).resolve()
            applied = subprocess.run(
                [
                    "git", "apply", "--unidiff-zero", "--whitespace=nowarn",
                    str(patch_path),
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            if applied.returncode:
                raise RuntimeError(
                    f"reference patch failed for {task['id']}: "
                    f"{(applied.stderr or applied.stdout).strip()}"
                )
            after = run_verifier(workspace, task, args.sandbox_image)
        output = "\n".join((before["stdout"], before["stderr"])).lower()
        discriminative = before["exit_code"] == 1 and "failed" in output
        fixed = after["ok"]
        rows.append({
            "task_id": task["id"],
            "pre_fix_failed": discriminative,
            "post_fix_passed": fixed,
            "pre_fix_exit_code": before["exit_code"],
            "post_fix_exit_code": after["exit_code"],
            "reference_fix_commit": task["reference_fix_commit"],
            "reference_patch": task["reference_patch"],
            "reference_patch_sha256": file_digest(patch_path),
            "fixture_tree_digest": tree_digest(fixture),
            "verifier_sha256": file_digest(ROOT / task["verifier_file"]),
            "output_tail": output[-1000:],
        })
        print(
            f"{task['id']}: "
            f"{'EXPECTED-FAIL' if discriminative else 'INVALID-PRE'} / "
            f"{'PASS-AFTER-PATCH' if fixed else 'INVALID-POST'}"
        )
    artifact = {
        "schema_version": "real-oss-preflight-v2",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "sandbox_image": args.sandbox_image,
        "sandbox_image_id": docker_image_id(args.sandbox_image),
        "manifest": str(args.manifest.relative_to(ROOT)),
        "manifest_sha256": file_digest(args.manifest),
        "summary": {
            "total": len(rows),
            "discriminative": sum(row["pre_fix_failed"] for row in rows),
            "reference_patch_passed": sum(row["post_fix_passed"] for row in rows),
        },
        "tasks": rows,
    }
    path = ROOT / "artifacts" / "real-oss-preflight.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return 0 if all(
        row["pre_fix_failed"] and row["post_fix_passed"] for row in rows
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
