#!/usr/bin/env python3
"""Run one frozen upstream bug through the current Pico Runtime."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from pico import OpenAICompatibleModelClient, Pico, SessionStore, WorkspaceContext
from pico.config import load_project_env, provider_env
from pico.evaluation.provenance import runtime_snapshot_id
from pico.sandbox import DockerSandbox, SandboxProfile, parse_command_invocation

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "validation" / "click_real_oss.json"


def load_task(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "click-real-oss-validation-v1":
        raise ValueError("unsupported Real OSS validation schema")
    task = payload.get("task")
    required = {
        "id", "prompt", "fixture_repo", "allowed_tools", "step_budget",
        "max_run_seconds", "required_change_globs", "allowed_change_globs",
        "forbidden_change_globs", "verifier_files", "verifier_command",
        "source_repository", "source_commit",
    }
    if not isinstance(task, dict) or required - set(task):
        raise ValueError(f"Real OSS task missing fields: {sorted(required - set(task or {}))}")
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


def changed_paths(before, after):
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )


def matches(path, patterns):
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def run_verifier(root, task):
    for item in task["verifier_files"]:
        source = (ROOT / item["source"]).resolve()
        target = (Path(root) / item["target"]).resolve()
        target.relative_to(Path(root).resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    argv, env = parse_command_invocation(task["verifier_command"])
    result = DockerSandbox(root).run(
        argv, cwd=root, timeout=90, env=env, profile=SandboxProfile.VERIFY
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
        f"- Event chain: `{'pass' if checks['event_chain']['ok'] else 'fail'}`",
        f"- Changed files: {', '.join(result['changed_files']) or 'none'}", "",
        "This is one end-to-end validation run, not a general success-rate claim.", "",
    ])
    Path(path).write_text(text, encoding="utf-8")


def run_validation(args):
    task = load_task(args.manifest)
    fixture = (ROOT / task["fixture_repo"]).resolve()
    if not fixture.is_dir():
        raise FileNotFoundError(
            f"missing fixture {fixture}; run scripts/materialize_real_oss.py"
        )
    runtime_metadata = git_metadata()
    require_clean_runtime(runtime_metadata)
    load_project_env(ROOT)
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

    client = OpenAICompatibleModelClient(model, base_url, api_key, args.temperature, args.timeout)
    agent = Pico(
        client,
        WorkspaceContext.build(workspace, repo_root_override=workspace),
        SessionStore(workspace / ".pico" / "sessions"),
        approval_policy="auto",
        max_steps=int(task["step_budget"]),
        max_new_tokens=args.max_new_tokens,
        run_timeout_seconds=int(task["max_run_seconds"]),
        allowed_tools=task["allowed_tools"],
        verification_command="",
    )
    started = time.monotonic()
    answer = agent.ask(task["prompt"])
    duration_ms = int((time.monotonic() - started) * 1000)
    after = file_snapshot(workspace)
    changed = changed_paths(before, after)
    forbidden = [
        path for path in changed if matches(path, task["forbidden_change_globs"])
    ]
    out_of_scope = [
        path for path in changed if not matches(path, task["allowed_change_globs"])
    ]
    missing_required = [
        pattern
        for pattern in task["required_change_globs"]
        if not any(fnmatch.fnmatch(path, pattern) for path in changed)
    ]
    mutation_scope = {
        "ok": not forbidden and not out_of_scope and not missing_required,
        "forbidden_changes": forbidden,
        "out_of_scope_changes": out_of_scope,
        "missing_required_globs": missing_required,
    }
    verifier = run_verifier(workspace, task)
    try:
        events = agent.run_store.read_events(agent.current_task_state.run_id)
        event_chain = {"ok": True, "event_count": len(events), "errors": []}
    except Exception as exc:  # noqa: BLE001 - audit failures are evidence
        event_chain = {"ok": False, "event_count": 0, "errors": [str(exc)]}
    passed = (
        agent.current_task_state.status == "completed"
        and mutation_scope["ok"]
        and verifier["ok"]
        and event_chain["ok"]
    )
    artifact = {
        "schema_version": "real-oss-validation-v2",
        "artifact_type": "real-oss-validation",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime_snapshot_id": runtime_snapshot_id(),
        "runtime": runtime_metadata,
        "model": model,
        "task": {
            "id": task["id"],
            "source_repository": task["source_repository"],
            "source_commit": task["source_commit"],
        },
        "result": {
            "passed": passed,
            "status": agent.current_task_state.status,
            "stop_reason": agent.current_task_state.stop_reason,
            "tool_steps": agent.current_task_state.tool_steps,
            "duration_ms": duration_ms,
            "changed_files": changed,
            "final_answer": answer,
            "run_id": agent.current_task_state.run_id,
            "checks": {
                "hidden_verifier": verifier,
                "mutation_scope": mutation_scope,
                "event_chain": event_chain,
            },
        },
    }
    Path(args.artifact).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    write_report(args.report, artifact)
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--artifact", type=Path, default=ROOT / "artifacts/real-oss-validation.json")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/real-oss-validation.md")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    artifact = run_validation(args)
    print(f"{artifact['task']['id']}: {'PASS' if artifact['result']['passed'] else 'FAIL'}")
    return 0 if artifact["result"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
