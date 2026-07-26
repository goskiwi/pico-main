#!/usr/bin/env python3
"""Run one real-OSS Pico task and surface its audit artifacts for a demo.

The demo intentionally uses the frozen Click task from ``real_oss_v1``. It is
small enough for a live walkthrough while still exercising the real model,
Docker boundary, post-run hidden verifier, run report, and optional Undo.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TASK_ID = "click_empty_bytes_echo"
FIXTURE_DIR = ROOT / "artifacts" / "real-oss-fixtures" / TASK_ID
SANDBOX_IMAGE = "pico-real-oss-v1:latest"


def _root_relative(path: Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def demo_paths(output_dir: Path) -> dict[str, Path]:
    output_dir = _root_relative(output_dir).resolve()
    workspace_root = output_dir / "workspaces"
    workspace = workspace_root / "rep-1" / "full" / TASK_ID / TASK_ID
    return {
        "output_dir": output_dir,
        "artifact": output_dir / "benchmark.json",
        "benchmark_report": output_dir / "benchmark.md",
        "workspace_root": workspace_root,
        "workspace": workspace,
    }


def build_benchmark_command(args, paths: dict[str, Path]) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_real_world_benchmark.py"),
        "--benchmark-path",
        str(ROOT / "benchmarks" / "real_oss_v1.json"),
        "--sandbox-image",
        SANDBOX_IMAGE,
        "--task",
        TASK_ID,
        "--variant",
        "full",
        "--repetitions",
        "1",
        "--max-new-tokens",
        str(args.max_new_tokens),
        "--verifier-timeout",
        str(args.verifier_timeout),
        "--artifact-path",
        str(paths["artifact"]),
        "--report-path",
        str(paths["benchmark_report"]),
        "--workspace-root",
        str(paths["workspace_root"]),
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.base_url:
        command.extend(["--base-url", args.base_url])
    return command


def _require_prerequisites():
    missing = []
    if not FIXTURE_DIR.is_dir():
        missing.append(
            "fixture missing; run: "
            "uv run python scripts/materialize_real_oss_v1.py "
            f"--task {TASK_ID}"
        )
    if shutil.which("docker") is None:
        missing.append("Docker is not available on PATH")
    elif subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE],
        capture_output=True,
        check=False,
    ).returncode:
        missing.append(
            "sandbox image missing; run: "
            "docker build -f Dockerfile.real-oss-v1 "
            f"-t {SANDBOX_IMAGE} ."
        )
    if missing:
        raise RuntimeError("Demo prerequisites are not ready:\n- " + "\n- ".join(missing))


def _load_demo_row(artifact_path: Path) -> dict:
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    rows = list(artifact.get("rows") or [])
    if len(rows) != 1:
        raise RuntimeError(f"demo expected exactly one benchmark row, found {len(rows)}")
    return rows[0]


def _undo_command(workspace: Path, run_id: str, *, dry_run: bool) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pico",
        "undo",
        "--cwd",
        str(workspace),
        "--run",
        run_id,
    ]
    if dry_run:
        command.append("--dry-run")
    return command


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Pico's frozen Click real-OSS demo and render its audit report."
    )
    parser.add_argument("--model", default=None, help="Optional model override.")
    parser.add_argument("--base-url", default=None, help="Optional Responses API base URL.")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--verifier-timeout", type=int, default=90)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/demo-real-oss-v1"),
        help="Ignored directory for the benchmark row, workspace, and rendered report.",
    )
    parser.add_argument(
        "--undo-after-run",
        action="store_true",
        help="After rendering the report, dry-run and apply this demo run's Undo journal.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.max_new_tokens < 1 or args.verifier_timeout < 1:
        raise SystemExit("--max-new-tokens and --verifier-timeout must be positive")
    _require_prerequisites()
    paths = demo_paths(args.output_dir)
    paths["output_dir"].mkdir(parents=True, exist_ok=True)

    command = build_benchmark_command(args, paths)
    print("Running frozen Click real-OSS demo:")
    print(shlex.join(command))
    subprocess.run(command, cwd=ROOT, check=True)

    row = _load_demo_row(paths["artifact"])
    run_dir = paths["workspace"] / ".pico" / "runs" / str(row["run_id"])
    if not run_dir.is_dir():
        raise RuntimeError(f"demo run artifact is missing: {run_dir}")
    renderer = [
        sys.executable,
        str(ROOT / "scripts" / "render_run_report.py"),
        str(run_dir),
    ]
    subprocess.run(renderer, cwd=ROOT, check=True)

    print(f"Hidden verifier: {'PASS' if row.get('passed') else 'FAIL'}")
    print(f"Benchmark report: {paths['benchmark_report']}")
    print(f"Run report: {run_dir / 'report.html'}")
    print(f"Workspace: {paths['workspace']}")
    preview_undo = _undo_command(paths["workspace"], str(row["run_id"]), dry_run=True)
    apply_undo = _undo_command(paths["workspace"], str(row["run_id"]), dry_run=False)
    print("Undo preview:")
    print(shlex.join(preview_undo))
    print("Undo apply:")
    print(shlex.join(apply_undo))

    if args.undo_after_run:
        subprocess.run(preview_undo, cwd=ROOT, check=True)
        subprocess.run(apply_undo, cwd=ROOT, check=True)
        print("Undo applied; the workspace is back at its frozen pre-Agent state.")

    return 0 if row.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
