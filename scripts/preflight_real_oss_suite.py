#!/usr/bin/env python3
"""Verify every hidden test fails against its frozen pre-fix checkout."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.materialize_real_oss import DEFAULT_MANIFEST, load_manifest
from scripts.run_real_oss_validation import (
    git_metadata,
    require_clean_runtime,
    run_verifier,
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
            result = run_verifier(workspace, task, args.sandbox_image)
        output = "\n".join((result["stdout"], result["stderr"])).lower()
        discriminative = not result["ok"] and "failed" in output
        rows.append({
            "task_id": task["id"],
            "pre_fix_failed": discriminative,
            "exit_code": result["exit_code"],
            "output_tail": output[-1000:],
        })
        print(f"{task['id']}: {'EXPECTED-FAIL' if discriminative else 'INVALID'}")
    artifact = {
        "schema_version": "real-oss-preflight-v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "sandbox_image": args.sandbox_image,
        "manifest": str(args.manifest.relative_to(ROOT)),
        "summary": {
            "total": len(rows),
            "discriminative": sum(row["pre_fix_failed"] for row in rows),
        },
        "tasks": rows,
    }
    path = ROOT / "artifacts" / "real-oss-preflight.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return 0 if all(row["pre_fix_failed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
