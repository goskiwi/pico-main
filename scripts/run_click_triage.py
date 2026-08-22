#!/usr/bin/env python3
"""Run the first real-model Pico Triage case against Click."""

from __future__ import annotations

import argparse
import json
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
from pico.sandbox import DockerSandbox, DockerSandboxConfig, parse_command_invocation
from scripts.materialize_real_oss import load_manifest as load_real_manifest
from scripts.run_official_public_tests import apply_patch
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

TASK_ID = "click_empty_bytes_echo"
VISIBLE_COMMAND = (
    "PYTHONPATH=src python -m pytest -q -p no:cacheprovider "
    "tests/test_utils.py::test_echo_custom_file"
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


def prepare_click_workspace(workspace, real_task, official_task):
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
    _git(workspace, "add", "--all")
    _git(workspace, "commit", "--quiet", "-m", "failing CI baseline")
    return _git(workspace, "rev-parse", "HEAD")


def run_visible_test(workspace, image):
    argv, env = parse_command_invocation(VISIBLE_COMMAND)
    result = DockerSandbox(
        workspace,
        DockerSandboxConfig(image=image),
    ).run(argv, cwd=workspace, timeout=120, env=env)
    return {
        "ok": result.returncode == 0 and not result.stop_reason,
        "exit_code": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "stop_reason": result.stop_reason,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--sandbox-image", default="pico/sandbox:latest")
    parser.add_argument("--hidden-image", default="pico/real-oss-suite:latest")
    parser.add_argument("--max-tool-executions", type=int, default=40)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=ROOT / "artifacts" / "triage-real-workspaces" / TASK_ID,
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=ROOT / "artifacts" / "triage-click-real.json",
    )
    args = parser.parse_args(argv)

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
    real_task = next(task for task in real_manifest["tasks"] if task["id"] == TASK_ID)
    official_manifest = load_official_manifest(
        ROOT / "validation" / "official_public_tests.json"
    )
    official_task = next(
        task for task in official_manifest["tasks"] if task["id"] == TASK_ID
    )
    baseline_sha = prepare_click_workspace(args.workspace, real_task, official_task)
    before = file_snapshot(args.workspace)
    initial = run_visible_test(args.workspace, args.sandbox_image)
    if initial["ok"]:
        raise RuntimeError("Click visible CI test did not fail before Triage")

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
            incident_id="click-empty-bytes-echo-ci",
            repository_root=args.workspace,
            revision=baseline_sha,
            failing_command=VISIBLE_COMMAND,
            verification_command=VISIBLE_COMMAND,
            ci_log="\n".join(
                part for part in (initial["stdout"], initial["stderr"]) if part
            ),
            issue=(
                "click.echo writing an empty bytes or bytearray value to a binary "
                "file raises TypeError when newline output is enabled."
            ),
            constraints=(
                "Do not modify tests",
                "Only modify production Python files under src/click",
                "Preserve existing text output behavior",
            ),
        )
    )
    visible_after = run_visible_test(args.workspace, args.sandbox_image)
    hidden = run_verifier(args.workspace, real_task, args.hidden_image)
    after = file_snapshot(args.workspace)
    changed = changed_paths(before, after)
    forbidden = [path for path in changed if matches(path, FORBIDDEN_CHANGE_GLOBS)]
    out_of_scope = [
        path for path in changed if not matches(path, real_task["allowed_change_globs"])
    ]
    passed = bool(
        report.status == "fixed"
        and report.reproduction.status == "reproduced"
        and visible_after["ok"]
        and hidden["ok"]
        and not forbidden
        and not out_of_scope
    )
    artifact = {
        "artifact_type": "pico-triage-real-case",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "case": {
            "id": TASK_ID,
            "source_repository": real_task["source_repository"],
            "source_commit": real_task["source_commit"],
            "failing_baseline_commit": baseline_sha,
        },
        "report": report.model_dump(mode="json"),
        "checks": {
            "initial_failure_reproduced": not initial["ok"],
            "visible_test_passed": visible_after["ok"],
            "hidden_verifier_passed": hidden["ok"],
            "scope_valid": not forbidden and not out_of_scope,
            "changed_paths": changed,
            "forbidden_changes": forbidden,
            "out_of_scope_changes": out_of_scope,
        },
        "passed": passed,
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(artifact, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
