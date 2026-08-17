#!/usr/bin/env python3
"""Run the five-repository frozen Real OSS suite sequentially."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_real_oss_validation import (
    DEFAULT_MANIFEST,
    git_metadata,
    require_clean_runtime,
    run_validation,
)


def load_task_ids(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "real-oss-suite-v2":
        raise ValueError("unsupported Real OSS suite schema")
    return [task["id"] for task in payload["tasks"]]


def retryable_infrastructure_error(error):
    text = str(error)
    return (
        "Could not reach the OpenAI-compatible backend" in text
        or "HTTP 408:" in text
        or "HTTP 429:" in text
        or any(f"HTTP {status}:" in text for status in range(500, 600))
    )


def prepare_task_root(path):
    path = Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def infrastructure_failure(task_id, runtime, model, error, attempts):
    return {
        "schema_version": "real-oss-validation-v3",
        "artifact_type": "real-oss-validation",
        "runtime": runtime,
        "model": model,
        "task": {"id": task_id},
        "suite_attempt": attempts,
        "result": {
            "passed": False,
            "status": "infrastructure_error",
            "stop_reason": "provider_unavailable",
            "tool_steps": 0,
            "duration_ms": 0,
            "changed_files": [],
            "final_answer": "",
            "run_id": "",
            "error": str(error),
            "checks": {},
        },
    }


def write_suite_report(path, artifact):
    rows = [
        "# Pico five-repository Real OSS suite",
        "",
        f"- Overall: **{artifact['summary']['passed']}/{artifact['summary']['total']}**",
        f"- Model: `{artifact['model']}`",
        f"- Runtime commit: `{artifact['runtime']['commit_sha']}`",
        "",
        "| Task | Result | Tool steps | Duration (s) | Changed files |",
        "|---|---:|---:|---:|---|",
    ]
    for item in artifact["tasks"]:
        result = item["result"]
        rows.append(
            f"| `{item['task']['id']}` | {'PASS' if result['passed'] else 'FAIL'} | "
            f"{result['tool_steps']} | {result['duration_ms'] / 1000:.1f} | "
            f"{', '.join(result['changed_files']) or 'none'} |"
        )
    rows.extend([
        "",
        "Each task starts from an exact pre-fix upstream commit. Hidden verifiers are injected only after the Agent stops. This fixed five-task run is reproducibility evidence, not a general coding-success estimate.",
        "",
    ])
    Path(path).write_text("\n".join(rows), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--sandbox-image", default="pico/real-oss-suite:latest")
    parser.add_argument("--task-attempts", type=int, default=2)
    parser.add_argument("--artifact", type=Path, default=ROOT / "artifacts/real-oss-suite-v2.json")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/real-oss-suite-v2.md")
    args = parser.parse_args(argv)

    runtime = git_metadata()
    require_clean_runtime(runtime)
    results = []
    task_root = ROOT / "artifacts" / "real-oss-suite-v2"
    prepare_task_root(task_root)
    for task_id in load_task_ids(args.manifest):
        print(f"{task_id}: running", flush=True)
        task_artifact = task_root / f"{task_id}.json"
        task_args = SimpleNamespace(
            manifest=args.manifest,
            task=task_id,
            artifact=task_artifact,
            report=task_root / f"{task_id}.md",
            model=args.model,
            base_url=args.base_url,
            max_new_tokens=args.max_new_tokens,
            max_steps=args.max_steps,
            temperature=args.temperature,
            timeout=args.timeout,
            sandbox_image=args.sandbox_image,
        )
        result = None
        final_error = None
        for suite_attempt in range(1, max(1, args.task_attempts) + 1):
            try:
                result = run_validation(task_args)
                result["suite_attempt"] = suite_attempt
                break
            except RuntimeError as exc:
                if not retryable_infrastructure_error(exc) or suite_attempt >= args.task_attempts:
                    final_error = exc
                    break
                print(
                    f"{task_id}: infrastructure retry {suite_attempt + 1}/{args.task_attempts}",
                    flush=True,
                )
        if result is None:
            result = infrastructure_failure(
                task_id, runtime, args.model, final_error, max(1, args.task_attempts)
            )
            task_artifact.parent.mkdir(parents=True, exist_ok=True)
            task_artifact.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        results.append(result)
        print(f"{task_id}: {'PASS' if result['result']['passed'] else 'FAIL'}", flush=True)

    passed = sum(item["result"]["passed"] for item in results)
    artifact = {
        "schema_version": "real-oss-suite-evidence-v2",
        "artifact_type": "real-oss-suite",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "tool_budget": (
            args.max_steps
            or json.loads(args.manifest.read_text(encoding="utf-8"))["tool_budget"]
        ),
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "tasks": results,
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    write_suite_report(args.report, artifact)
    print(f"suite: {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
