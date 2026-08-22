#!/usr/bin/env python3
"""Run one frozen upstream bug through the current Pico Runtime."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico import (
    OpenAICompatibleModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.config import load_project_env, provider_env
from pico.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    shell_argv,
)
from scripts.materialize_real_oss import DEFAULT_MANIFEST, load_manifest, tree_digest

ALLOWED_TOOLS = (
    "list_files",
    "read_file",
    "read_artifact",
    "search",
    "run_shell",
    "write_file",
    "patch_file",
)
FORBIDDEN_CHANGE_GLOBS = (
    "tests/**",
    "test/**",
    "testing/**",
    ".pico_hidden_verifier/**",
)


def load_task(path, task_id):
    payload = load_manifest(path)
    tasks = payload["tasks"]
    task = next((item for item in tasks if item.get("id") == task_id), None)
    if task is None:
        raise ValueError(f"unknown Real OSS task: {task_id}")
    task = dict(task)
    task["tool_budget"] = int(payload.get("tool_budget", 0))
    if task["tool_budget"] < 1:
        raise ValueError("Real OSS suite requires one positive uniform tool_budget")
    return task


def file_snapshot(root):
    root = Path(root)
    snapshot = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if relative.parts[0] in {".git", ".pico", ".pico_hidden_verifier"}:
            continue
        if "__pycache__" in relative.parts:
            continue
        snapshot[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def file_digest(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def docker_image_id(image):
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(
            f"could not resolve Docker image id for {image}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def changed_paths(before, after):
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


def matches(path, patterns):
    return any(
        fnmatch.fnmatch(path, pattern)
        or ("/**/" in pattern and fnmatch.fnmatch(path, pattern.replace("/**/", "/")))
        for pattern in patterns
    )


def run_verifier(root, task, sandbox_image):
    source = (ROOT / task["verifier_file"]).resolve()
    target = (Path(root) / ".pico_hidden_verifier" / "test_hidden.py").resolve()
    target.relative_to(Path(root).resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    result = DockerSandbox(
        root,
        DockerSandboxConfig(image=sandbox_image),
    ).run(
        shell_argv(task["verifier_command"]), cwd=root, timeout=90, env={}
    )
    return {
        "ok": result.returncode == 0 and not result.stop_reason,
        "exit_code": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "stop_reason": result.stop_reason,
    }


def git_metadata():
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "branch": git("branch", "--show-current"),
        "commit_sha": git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(git("status", "--porcelain")),
    }


def require_clean_runtime(metadata):
    if metadata["working_tree_dirty"]:
        raise RuntimeError(
            "Real OSS validation requires a clean worktree; commit the Runtime first"
        )


def provider_continuation_check(events):
    turns = [
        entry for entry in events if entry.kind == "turn_metrics"
    ]
    reused_turns = sum(
        bool(entry.payload.get("prompt_reused")) for entry in turns
    )
    reset_count = sum(
        entry.kind == "provider_session_reset" for entry in events
    )
    return {
        "ok": len(turns) >= 2 and reused_turns >= 1,
        "prompt_builds": len(turns),
        "reused_turns": reused_turns,
        "provider_session_resets": reset_count,
    }


def write_report(path, artifact):
    result = artifact["result"]
    checks = result["checks"]
    text = "\n".join([
        "# Pico Real OSS validation", "",
        f"- Verdict: **{'PASS' if result['passed'] else 'FAIL'}**",
        f"- Task: `{artifact['task']['id']}`",
        f"- Model: `{artifact['model']}`",
        f"- Runtime status: `{result['status']}`",
        f"- Hidden verifier: `{'pass' if checks['hidden_verifier']['ok'] else 'fail'}`",
        f"- Mutation scope: `{'pass' if checks['mutation_scope']['ok'] else 'fail'}`",
        f"- Run Log: `{'pass' if checks['run_log']['ok'] else 'fail'}`",
        f"- Provider continuation: `{'pass' if checks['provider_continuation']['ok'] else 'fail'}`",
        f"- Changed files: {', '.join(result['changed_files']) or 'none'}", "",
        "This is one end-to-end validation run, not a general success-rate claim.", "",
    ])
    Path(path).write_text(text, encoding="utf-8")


def run_validation(args):
    task = load_task(args.manifest, args.task)
    fixture = (ROOT / task["fixture_repo"]).resolve()
    if not fixture.is_dir():
        raise FileNotFoundError(
            f"missing fixture {fixture}; run scripts/materialize_real_oss.py"
        )
    runtime_metadata = git_metadata()
    require_clean_runtime(runtime_metadata)
    load_project_env(ROOT, boundary=ROOT)
    model = args.model or provider_env("PICO_OPENAI_MODEL", "gpt-5.4")
    base_url = args.base_url or provider_env(
        "PICO_OPENAI_API_BASE", "https://api.openai.com/v1"
    )
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("PICO_OPENAI_API_KEY is required")

    workspace = (ROOT / "artifacts" / "real-oss-workspaces" / task["id"] / "current").resolve()
    workspace_root = (ROOT / "artifacts" / "real-oss-workspaces").resolve()
    workspace.relative_to(workspace_root)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture, workspace)
    before = file_snapshot(workspace)
    provenance = {
        "manifest_sha256": file_digest(args.manifest),
        "fixture_tree_digest": tree_digest(fixture),
        "verifier_sha256": file_digest(ROOT / task["verifier_file"]),
        "reference_patch_sha256": file_digest(ROOT / task["reference_patch"]),
        "reference_fix_commit": task["reference_fix_commit"],
        "sandbox_image": args.sandbox_image,
        "sandbox_image_id": docker_image_id(args.sandbox_image),
        "tool_budget": int(args.max_tool_executions or task["tool_budget"]),
    }

    client = OpenAICompatibleModelClient(model, base_url, api_key, args.temperature, args.timeout)
    agent = Pico(
        client,
        WorkspaceContext.build(workspace, repo_root_override=workspace),
        SessionStore(workspace / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            max_tool_executions=int(args.max_tool_executions or task["tool_budget"]),
            max_new_tokens=args.max_new_tokens,
            run_timeout_seconds=360,
            allowed_tools=ALLOWED_TOOLS,
            sandbox_image=args.sandbox_image,
            verification_command="",
        ),
    )
    started = time.monotonic()
    answer = agent.ask(task["prompt"])
    duration_ms = int((time.monotonic() - started) * 1000)
    after = file_snapshot(workspace)
    changed = changed_paths(before, after)
    forbidden = [
        path for path in changed if matches(path, FORBIDDEN_CHANGE_GLOBS)
    ]
    out_of_scope = [
        path for path in changed if not matches(path, task["allowed_change_globs"])
    ]
    missing_required = [
        pattern
        for pattern in task["required_change_globs"]
        if not any(matches(path, (pattern,)) for path in changed)
    ]
    mutation_scope = {
        "ok": not forbidden and not out_of_scope and not missing_required,
        "forbidden_changes": forbidden,
        "out_of_scope_changes": out_of_scope,
        "missing_required_globs": missing_required,
    }
    verifier = run_verifier(workspace, task, args.sandbox_image)
    try:
        events = agent.dependencies.run_store.read_events(agent.run.task_state.run_id)
        run_log = {"ok": True, "event_count": len(events), "errors": []}
    except Exception as exc:  # noqa: BLE001 - audit failures are evidence
        run_log = {"ok": False, "event_count": 0, "errors": [str(exc)]}
        events = []
    provider_continuation = provider_continuation_check(events)
    passed = (
        agent.run.task_state.status == "completed"
        and mutation_scope["ok"]
        and verifier["ok"]
        and run_log["ok"]
        and provider_continuation["ok"]
    )
    artifact = {
        "schema_version": "real-oss-validation-v3",
        "artifact_type": "real-oss-validation",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime_metadata,
        "model": model,
        "task": {
            "id": task["id"],
            "source_repository": task["source_repository"],
            "source_commit": task["source_commit"],
        },
        "provenance": provenance,
        "result": {
            "passed": passed,
            "status": agent.run.task_state.status,
            "stop_reason": agent.run.task_state.stop_reason,
            "executed_tool_count": agent.run.task_state.executed_tool_count,
            "duration_ms": duration_ms,
            "changed_files": changed,
            "final_answer": answer,
            "run_id": agent.run.task_state.run_id,
            "checks": {
                "hidden_verifier": verifier,
                "mutation_scope": mutation_scope,
                "run_log": run_log,
                "provider_continuation": provider_continuation,
            },
        },
    }
    Path(args.artifact).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.artifact).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    write_report(args.report, artifact)
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--task", default="click_empty_bytes_echo")
    parser.add_argument("--artifact", type=Path, default=ROOT / "artifacts/real-oss-validation.json")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/real-oss-validation.md")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--max-tool-executions", type=int)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--sandbox-image", default="pico/real-oss-suite:latest")
    args = parser.parse_args(argv)
    artifact = run_validation(args)
    print(f"{artifact['task']['id']}: {'PASS' if artifact['result']['passed'] else 'FAIL'}")
    return 0 if artifact["result"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
