#!/usr/bin/env python3
"""Run one frozen Real OSS task through Pico Triage."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from applications.triage import TriageCase, TriageWorkflow
from pico import OpenAICompatibleModelClient, PicoConfig
from pico.config import load_project_env, provider_env
from pico.run_store import RunStore
from pico.sandbox import DockerSandbox, DockerSandboxConfig, shell_argv
from scripts.materialize_real_oss import load_manifest as load_real_manifest
from scripts.run_official_public_tests import apply_patch, expected_failure
from scripts.run_official_public_tests import load_manifest as load_official_manifest
from scripts.run_real_oss_validation import (
    FORBIDDEN_CHANGE_GLOBS,
    changed_paths,
    file_snapshot,
    git_metadata,
    matches,
    require_clean_runtime,
    run_verifier,
)


def _git(root, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def prepare_triage_workspace(workspace, real_task, official_task):
    workspace = Path(workspace).resolve()
    fixture = (ROOT / real_task["fixture_repo"]).resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture, workspace)
    apply_patch(workspace, ROOT / official_task["official_test_patch"])
    gitignore = workspace / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if ".pico/" not in existing.splitlines():
        gitignore.write_text(existing.rstrip() + "\n.pico/\n", encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "user.name", "Pico Triage")
    _git(workspace, "config", "user.email", "pico-triage@example.invalid")
    # Frozen fixtures may include ignored generated source required for imports.
    # Track the complete controlled fixture so Implement worktrees reproduce the
    # same runnable baseline as the Parent workspace.
    _git(workspace, "add", "-f", "--all")
    _git(workspace, "commit", "--quiet", "-m", "failing CI baseline")
    return _git(workspace, "rev-parse", "HEAD")


def visible_command(official_task):
    nodes = " ".join(shlex.quote(item) for item in official_task["official_test_nodes"])
    return f"PYTHONPATH=src python -m pytest -q --tb=short -p no:cacheprovider {nodes}"


def run_visible_test(workspace, image, command):
    result = DockerSandbox(
        workspace,
        DockerSandboxConfig(image=image),
    ).run(shell_argv(command), cwd=workspace, timeout=120, env={})
    return {
        "ok": result.returncode == 0 and not result.stop_reason,
        "exit_code": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "stop_reason": result.stop_reason,
    }


def collect_run_metrics(workspace, report):
    store = RunStore(Path(workspace) / ".pico" / "runs")
    events = store.read_events(report.run_id)
    projection = store.replay(report.run_id)
    parent_run = (events, projection)
    child_runs = load_child_runs(workspace, report.run_id)
    return {
        "wall_duration_ms": projection.run_duration_ms,
        "child_count": len(child_runs),
        "parent": summarize_runs((parent_run,)),
        "children": summarize_runs(child_runs),
        "total": summarize_runs((parent_run, *child_runs)),
    }


def load_child_runs(workspace, parent_run_id):
    root = (
        Path(workspace)
        / ".pico"
        / "runs"
        / parent_run_id
        / "subagents"
    )
    runs = []
    for path in sorted(root.glob("*/runs/*/events.jsonl")):
        run_id = path.parent.name
        store = RunStore(path.parent.parent)
        events = store.read_events(run_id)
        runs.append((events, store.replay(run_id)))
    return tuple(runs)


def summarize_runs(runs):
    runs = tuple(runs)
    events = [entry for run_events, _projection in runs for entry in run_events]
    projections = [projection for _events, projection in runs]
    durations = [projection.run_duration_ms for projection in projections]
    status_counts = {}
    for projection in projections:
        status_counts[projection.status] = status_counts.get(projection.status, 0) + 1
    turns = [entry for entry in events if entry.kind == "turn_metrics"]
    return {
        "run_count": len(runs),
        "model_request_count": sum(
            projection.model_request_count for projection in projections
        ),
        "executed_tool_count": sum(
            projection.executed_tool_count for projection in projections
        ),
        "sum_duration_ms": sum(durations),
        "max_duration_ms": max(durations, default=0),
        "status_counts": status_counts,
        **usage_metrics(turns),
    }


def usage_metrics(turns):
    completions = [
        entry.payload.get("completion_metadata", {}) for entry in turns
    ]
    gross = sum(int(item.get("input_tokens") or 0) for item in completions)
    output = sum(int(item.get("output_tokens") or 0) for item in completions)
    cache_complete = bool(completions) and all(
        isinstance(item.get("cached_tokens"), int) for item in completions
    )
    cached = (
        sum(int(item["cached_tokens"]) for item in completions)
        if cache_complete
        else None
    )
    return {
        "gross_input_tokens": gross,
        "cached_input_tokens": cached,
        "uncached_input_tokens": gross - cached if cached is not None else None,
        "cache_reporting_complete": cache_complete,
        "output_tokens": output,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="click_empty_bytes_echo")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--sandbox-image", default="pico/official-public-tests:latest")
    parser.add_argument("--hidden-image", default="pico/real-oss-suite:latest")
    parser.add_argument("--max-tool-executions", type=int, default=40)
    parser.add_argument(
        "--workspace",
        type=Path,
    )
    parser.add_argument(
        "--artifact",
        type=Path,
    )
    parser.add_argument(
        "--patch",
        type=Path,
    )
    args = parser.parse_args(argv)
    project_name = args.task.split("_", 1)[0]
    args.workspace = args.workspace or (
        ROOT / "artifacts" / "triage-real-workspaces" / args.task
    )
    args.artifact = args.artifact or (
        ROOT / "artifacts" / f"triage-{project_name}-real.json"
    )
    args.patch = args.patch or (
        ROOT / "artifacts" / f"triage-{project_name}-real.patch"
    )

    runtime = git_metadata()
    require_clean_runtime(runtime)
    load_project_env(ROOT, boundary=ROOT)
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("PICO_OPENAI_API_KEY is required")
    base_url = args.base_url or provider_env(
        "PICO_OPENAI_API_BASE", "https://api.openai.com/v1"
    )
    real_manifest = load_real_manifest(ROOT / "validation" / "real_oss_suite.json")
    real_task = next(
        (task for task in real_manifest["tasks"] if task["id"] == args.task),
        None,
    )
    official_manifest = load_official_manifest(
        ROOT / "validation" / "official_public_tests.json"
    )
    official_task = next(
        (task for task in official_manifest["tasks"] if task["id"] == args.task),
        None,
    )
    if real_task is None or official_task is None:
        raise ValueError(f"unknown Real Triage task: {args.task}")
    if official_task["pre_fix_expected"] != "fail":
        raise ValueError(
            f"{args.task} has no discriminative visible Official Test"
        )
    command = visible_command(official_task)
    baseline_sha = prepare_triage_workspace(args.workspace, real_task, official_task)
    before = file_snapshot(args.workspace)
    initial = run_visible_test(args.workspace, args.sandbox_image, command)
    if not expected_failure(initial):
        raise RuntimeError(
            f"{args.task} visible CI test did not fail discriminatively before Triage"
        )

    def model_client():
        return OpenAICompatibleModelClient(
            args.model,
            base_url,
            api_key,
            args.temperature,
            args.timeout,
        )

    sandbox_config = DockerSandboxConfig(image=args.sandbox_image)
    report = TriageWorkflow(
        model_client(),
        config=PicoConfig(
            approval_policy="auto",
            max_tool_executions=args.max_tool_executions,
            sandbox_image=args.sandbox_image,
        ),
        subagent_model_client_factory=lambda _spec: model_client(),
        sandbox_factory=lambda root: DockerSandbox(root, sandbox_config),
    ).run(
        TriageCase(
            incident_id=args.task.replace("_", "-") + "-ci",
            repository_root=args.workspace,
            revision=baseline_sha,
            failing_command=command,
            verification_command=command,
            ci_log="\n".join(
                part for part in (initial["stdout"], initial["stderr"]) if part
            ),
            issue=real_task["prompt"],
            constraints=(
                "Do not modify tests",
                "Only modify paths matching: "
                + ", ".join(real_task["allowed_change_globs"]),
                "Preserve public behavior outside the reported failure",
            ),
        )
    )
    visible_after = run_visible_test(args.workspace, args.sandbox_image, command)
    hidden = run_verifier(args.workspace, real_task, args.hidden_image)
    after = file_snapshot(args.workspace)
    changed = changed_paths(before, after)
    patch_text = _git(args.workspace, "diff", "--binary", "--unified=1", "HEAD")
    forbidden = [path for path in changed if matches(path, FORBIDDEN_CHANGE_GLOBS)]
    out_of_scope = [
        path for path in changed if not matches(path, real_task["allowed_change_globs"])
    ]
    root_files = tuple(report.root_cause.files)
    root_cause_top1 = bool(root_files and root_files[0] in changed)
    root_cause_top3 = bool(set(root_files[:3]) & set(changed))
    metrics = collect_run_metrics(args.workspace, report)
    passed = bool(
        report.status == "fixed"
        and report.reproduction.status == "reproduced"
        and root_cause_top3
        and visible_after["ok"]
        and hidden["ok"]
        and patch_text
        and not forbidden
        and not out_of_scope
    )
    artifact = {
        "artifact_type": "pico-triage-real-case",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "case": {
            "id": args.task,
            "source_repository": real_task["source_repository"],
            "source_commit": real_task["source_commit"],
            "failing_baseline_commit": baseline_sha,
        },
        "metrics": metrics,
        "report": report.model_dump(mode="json"),
        "checks": {
            "initial_failure_reproduced": not initial["ok"],
            "visible_test_passed": visible_after["ok"],
            "hidden_verifier_passed": hidden["ok"],
            "patch_recorded": bool(patch_text),
            "root_cause_top1": root_cause_top1,
            "root_cause_top3": root_cause_top3,
            "scope_valid": not forbidden and not out_of_scope,
            "changed_paths": changed,
            "forbidden_changes": forbidden,
            "out_of_scope_changes": out_of_scope,
        },
        "passed": passed,
    }
    args.patch.parent.mkdir(parents=True, exist_ok=True)
    args.patch.write_text(patch_text.rstrip() + "\n", encoding="utf-8")
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
