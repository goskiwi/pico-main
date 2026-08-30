#!/usr/bin/env python3
"""Compare bounded Runtime fallback and historical-only semantic compaction."""

from __future__ import annotations

import argparse
import json
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
from pico.providers.clients import DEFAULT_OPENAI_BASE_URL
from pico.sandbox import DockerSandbox, DockerSandboxConfig
from scripts.real_case_support import git_metadata, require_clean_runtime
from scripts.run_real_compaction import (
    CONTROLLED_CONTEXT_LIMIT,
    EVIDENCE_COUNT,
    HIDDEN_COMMAND,
    TARGET_PATH,
    VISIBLE_COMMAND,
    _git,
    analyze_run,
    build_prompt,
    prepare_workspace,
    run_command,
)

CRITICAL_TOKEN = "ORBIT-DELTA-7319"


def prepare_ab_workspace(workspace):
    workspace = prepare_workspace(workspace)
    path = workspace / "evidence" / "segment_01.md"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "\nFINAL_RESPONSE_TOKEN: "
            + CRITICAL_TOKEN
            + "\nThis literal must appear in the final answer.\n"
        )
    _git(workspace, "add", "-f", "--all")
    _git(workspace, "commit", "--quiet", "-m", "add critical-context condition")
    return workspace


def ab_prompt():
    return (
        build_prompt()
        + "\nThe first evidence file contains a FINAL_RESPONSE_TOKEN near its end. "
        "Do not copy that token into WorkingState or any tool argument. Preserve it only "
        "in conversational context, and include it verbatim in the final answer.\n"
    )


def model_client(model, base_url, api_key, temperature, timeout):
    return OpenAICompatibleModelClient(
        model,
        base_url,
        api_key,
        temperature,
        timeout,
    )


def run_variant(args, variant, api_key, base_url, run_group):
    workspace = prepare_ab_workspace(
        args.workspace_root / f"semantic-compaction-{run_group}-{variant}"
    )
    print(f"[semantic-compaction] starting {variant}", file=sys.stderr, flush=True)
    initial = run_command(workspace, args.sandbox_image, VISIBLE_COMMAND)
    if initial["infrastructure_error"]:
        raise RuntimeError(f"{variant} baseline sandbox could not start")
    if initial["ok"]:
        raise RuntimeError(f"{variant} baseline must fail before the Agent runs")
    agent = Pico(
        model_client=model_client(
            args.model, base_url, api_key, args.temperature, args.timeout
        ),
        workspace=WorkspaceContext.build(workspace),
        session_store=SessionStore(workspace / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            allowed_tools=("read_file", "edit_file", "update_working_state"),
            allowed_write_paths=(TARGET_PATH,),
            max_tool_executions=18,
            max_new_tokens=1024,
            run_timeout_seconds=args.run_timeout_seconds,
            provider_context_limit_tokens=CONTROLLED_CONTEXT_LIMIT,
            compaction_reserve_tokens=8192,
            compaction_keep_recent_tokens=8000,
            sandbox_image=args.sandbox_image,
            verification_command=VISIBLE_COMMAND,
        ),
        sandbox=DockerSandbox(
            workspace,
            DockerSandboxConfig(image=args.sandbox_image),
        ),
    )
    if variant == "fallback":
        agent.prompt.context.semantic_summarizer = None
    summarizer = agent.prompt.context.semantic_summarizer
    started = time.monotonic()
    outcome = agent.ask(
        ab_prompt(),
        task_kind="modify",
        requires_workspace_change=True,
        requires_verification=True,
    )
    wall_duration_ms = int((time.monotonic() - started) * 1000)
    events = agent.dependencies.run_store.read_events(outcome.run_id)
    analysis = analyze_run(events, agent.run.task)
    visible = run_command(workspace, args.sandbox_image, VISIBLE_COMMAND)
    hidden = run_command(workspace, args.sandbox_image, HIDDEN_COMMAND)
    patch_text = _git(workspace, "diff", "--binary", "--unified=1", "HEAD")
    changed_paths = _git(workspace, "diff", "--name-only", "HEAD").splitlines()
    token_in_working_state = CRITICAL_TOKEN in json.dumps(
        analysis["working_state"], ensure_ascii=False
    )
    checks = {
        "initial_failure_reproduced": not initial["ok"],
        "context_reduction_triggered": bool(analysis["compactions"]),
        "provider_session_reset": analysis["provider_session_reset_count"] >= 1,
        "evidence_read_once_in_order": analysis["evidence_read_paths"]
        == [
            f"evidence/segment_{index:02d}.md"
            for index in range(1, EVIDENCE_COUNT + 1)
        ],
        "token_not_in_working_state": not token_in_working_state,
        "critical_token_retained": CRITICAL_TOKEN in outcome.answer,
        "single_successful_mutation": analysis["successful_mutation_count"] == 1,
        "visible_verifier_passed": visible["ok"],
        "hidden_verifier_passed": hidden["ok"],
        "scope_valid": changed_paths == [TARGET_PATH],
        "patch_recorded": bool(patch_text),
    }
    result = {
        "variant": variant,
        "run_id": outcome.run_id,
        "wall_duration_ms": wall_duration_ms,
        "final_answer": outcome.answer,
        "analysis": analysis,
        "summary_calls": summarizer.calls if summarizer is not None else [],
        "checks": checks,
        "changed_paths": changed_paths,
        "passed_task": all(
            value
            for name, value in checks.items()
            if name != "critical_token_retained"
        ),
    }
    print(
        f"[semantic-compaction] completed {variant} in {wall_duration_ms} ms",
        file=sys.stderr,
        flush=True,
    )
    return result


def write_artifact(path, artifact):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--run-timeout-seconds", type=int, default=900)
    parser.add_argument("--sandbox-image", default="pico/sandbox:latest")
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=ROOT / "artifacts",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=ROOT / "artifacts" / "semantic-compaction-ab.json",
    )
    args = parser.parse_args(argv)

    runtime = git_metadata()
    require_clean_runtime(runtime)
    load_project_env(ROOT, boundary=ROOT)
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("PICO_OPENAI_API_KEY is required")
    base_url = args.base_url or provider_env(
        "PICO_OPENAI_API_BASE", DEFAULT_OPENAI_BASE_URL
    )
    run_group = datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")
    baseline = run_variant(args, "fallback", api_key, base_url, run_group)
    partial = {
        "artifact_type": "pico-semantic-compaction-ab",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "provider_base_url": base_url,
        "critical_token": CRITICAL_TOKEN,
        "run_group": run_group,
        "variants": {"fallback": baseline},
        "comparison": {"status": "semantic_pending"},
        "passed": False,
    }
    write_artifact(args.artifact, partial)
    semantic = run_variant(args, "semantic", api_key, base_url, run_group)
    comparison = {
        "baseline_task_passed": baseline["passed_task"],
        "semantic_task_passed": semantic["passed_task"],
        "baseline_critical_token_retained": baseline["checks"][
            "critical_token_retained"
        ],
        "semantic_critical_token_retained": semantic["checks"][
            "critical_token_retained"
        ],
        "semantic_summary_request_count": len(semantic["summary_calls"]),
        "wall_duration_delta_ms": (
            semantic["wall_duration_ms"] - baseline["wall_duration_ms"]
        ),
    }
    passed = bool(
        comparison["baseline_task_passed"]
        and comparison["semantic_task_passed"]
        and not comparison["baseline_critical_token_retained"]
        and comparison["semantic_critical_token_retained"]
        and comparison["semantic_summary_request_count"] >= 1
    )
    artifact = {
        "artifact_type": "pico-semantic-compaction-ab",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "provider_base_url": base_url,
        "critical_token": CRITICAL_TOKEN,
        "run_group": run_group,
        "variants": {"fallback": baseline, "semantic": semantic},
        "comparison": comparison,
        "passed": passed,
    }
    write_artifact(args.artifact, artifact)
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
