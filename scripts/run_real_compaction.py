#!/usr/bin/env python3
"""Run one controlled real-LLM compaction and continuation evaluation."""

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

from pico import (
    OpenAICompatibleModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.command_runner import CommandRunner, shell_argv
from pico.config import load_project_env, provider_env
from pico.providers.clients import DEFAULT_OPENAI_BASE_URL
from scripts.real_case_support import git_metadata, require_clean_runtime

EVIDENCE_COUNT = 12
CONTROLLED_CONTEXT_LIMIT = 28_000
TARGET_PATH = "src/normalizer.py"
VISIBLE_COMMAND = (
    "PYTHONPATH=. python -c \"from src.normalizer import normalize_label; "
    "assert normalize_label('  Priority   Queue ') == 'priority-queue'\""
)
HIDDEN_COMMAND = (
    "PYTHONPATH=. python -c \"from src.normalizer import normalize_label as n; "
    "assert n('\\tAlpha\\nBeta\\t') == 'alpha-beta'; "
    "assert n('single') == 'single'; assert n('  Already-Hyphenated  ') "
    "== 'already-hyphenated'\""
)
FACTS = (
    "The only permitted production change is src/normalizer.py.",
    "Leading and trailing whitespace must be discarded.",
    "Every run of internal whitespace must become one ASCII hyphen.",
    "Alphabetic characters must be normalized to lowercase.",
    "Existing non-whitespace punctuation, including hyphens, must be preserved.",
    "The public function name and one-argument signature must not change.",
    "A label containing one word must remain that word after lowercasing.",
    "The implementation must not introduce a dependency or regular expression.",
    "The intended implementation can be expressed with strip, lower, split, and join.",
    "No evidence file or test file may be modified.",
    "The final implementation must be deterministic for tabs and newlines too.",
    "After the edit, submit completion so the Runtime verifier executes.",
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


def prepare_workspace(workspace):
    workspace = Path(workspace).resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    (workspace / "src").mkdir(parents=True)
    (workspace / "evidence").mkdir()
    (workspace / "src" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / TARGET_PATH).write_text(
        "def normalize_label(value: str) -> str:\n"
        "    return value.strip().lower()\n",
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(".pico/\n", encoding="utf-8")
    for index, fact in enumerate(FACTS, start=1):
        filler = "\n".join(
            f"audit record {line:03d}: historical sample for segment {index:02d}; "
            "retained only to exercise bounded context continuation."
            for line in range(1, 76)
        )
        (workspace / "evidence" / f"segment_{index:02d}.md").write_text(
            f"# Evidence segment {index:02d}\n\nCritical fact: {fact}\n\n{filler}\n",
            encoding="utf-8",
        )
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "user.name", "Pico Compaction Evaluation")
    _git(workspace, "config", "user.email", "pico-compaction@example.invalid")
    _git(workspace, "add", "--all")
    _git(workspace, "commit", "--quiet", "-m", "controlled failing baseline")
    return workspace


def build_prompt():
    files = "\n".join(
        f"- evidence/segment_{index:02d}.md" for index in range(1, EVIDENCE_COUNT + 1)
    )
    return f"""Complete a controlled long-context coding task.

First call update_working_state. Record these constraints: read every listed evidence
file exactly once in order, modify only {TARGET_PATH}, and preserve the public API.
Record a next step to read all evidence before editing.

Then read every file below with one read_file call per file, using start_line=1 and end_line=200.
Do not use Search, List, or delegation:
{files}

After all evidence is read, call update_working_state again to record the evidence-backed
normalization decision and the concrete edit as the next step. Read {TARGET_PATH}, apply
the smallest exact patch, and call submit_final. The Runtime owns verification.
"""


def run_command(workspace, command):
    result = CommandRunner(workspace).run(
        shell_argv(command), cwd=workspace, timeout=120, env={}
    )
    return {
        "ok": result.returncode == 0 and not result.stop_reason,
        "infrastructure_error": result.infrastructure_error,
        "exit_code": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "stop_reason": result.stop_reason,
    }


def analyze_run(events, task_state):
    kinds = [entry.kind for entry in events]
    turns = [entry for entry in events if entry.kind == "turn_metrics"]
    input_tokens = [
        entry.payload.get("completion_metadata", {}).get("input_tokens")
        for entry in turns
    ]
    input_tokens = [value for value in input_tokens if isinstance(value, int)]
    compaction_turns = [
        entry.payload.get("prompt_metadata", {}).get("compaction")
        for entry in turns
        if entry.payload.get("prompt_metadata", {}).get("compaction")
    ]
    successful_mutations = [
        entry
        for entry in events
        if entry.kind == "tool_result"
        and entry.name in {"write_file", "edit_file"}
        and entry.outcome_status == "success"
        and entry.side_effect_state == "changed"
    ]
    evidence_read_paths = [
        entry.args.get("path")
        for entry in events
        if entry.kind == "assistant_tool_call"
        and entry.name == "read_file"
        and str(entry.args.get("path", "")).startswith("evidence/")
    ]
    state = task_state.working
    return {
        "model_request_count": len(turns),
        "max_single_request_input_tokens": max(input_tokens, default=0),
        "compaction_count": kinds.count("compaction"),
        "provider_session_reset_count": kinds.count("provider_session_reset"),
        "resume_count": kinds.count("run_resumed"),
        "successful_mutation_count": len(successful_mutations),
        "evidence_read_paths": evidence_read_paths,
        "compactions": compaction_turns,
        "working_state": state.to_dict(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--run-timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=ROOT / "artifacts" / "real-compaction-workspace",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=ROOT / "artifacts" / "real-compaction.json",
    )
    parser.add_argument(
        "--patch",
        type=Path,
        default=ROOT / "artifacts" / "real-compaction.patch",
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
    workspace = prepare_workspace(args.workspace)
    initial = run_command(workspace, VISIBLE_COMMAND)
    if initial["infrastructure_error"]:
        raise RuntimeError("controlled baseline command could not start")
    if initial["ok"]:
        raise RuntimeError("controlled baseline must fail before the Agent runs")

    client = OpenAICompatibleModelClient(
        args.model,
        base_url,
        api_key,
        args.temperature,
        args.timeout,
    )
    agent = Pico(
        model_client=client,
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
            verification_command=VISIBLE_COMMAND,
        ),
        command_runner=CommandRunner(workspace),
    )
    outcome = agent._ask_with_intent(
        build_prompt(),
        intent="modify",
    )
    run_id = outcome.run_id
    events = agent.dependencies.run_store.read_events(run_id)
    analysis = analyze_run(events, agent.run.task)
    visible = run_command(workspace, VISIBLE_COMMAND)
    hidden = run_command(workspace, HIDDEN_COMMAND)
    patch_text = _git(workspace, "diff", "--binary", "--unified=1", "HEAD")
    changed_paths = _git(workspace, "diff", "--name-only", "HEAD").splitlines()

    checks = {
        "initial_failure_reproduced": not initial["ok"],
        "compaction_triggered": analysis["compaction_count"] >= 1,
        "provider_session_reset": analysis["provider_session_reset_count"] >= 1,
        "working_state_preserved": bool(
            analysis["working_state"]["constraints"]
            and analysis["working_state"]["decisions"]
            and analysis["working_state"]["next_steps"]
        ),
        "evidence_read_once_in_order": analysis["evidence_read_paths"]
        == [
            f"evidence/segment_{index:02d}.md"
            for index in range(1, EVIDENCE_COUNT + 1)
        ],
        "single_successful_mutation": analysis["successful_mutation_count"] == 1,
        "visible_verifier_passed": visible["ok"],
        "hidden_verifier_passed": hidden["ok"],
        "scope_valid": changed_paths == [TARGET_PATH],
        "patch_recorded": bool(patch_text),
    }
    passed = all(checks.values())
    artifact = {
        "artifact_type": "pico-real-compaction",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "policy": {
            "provider_base_url": base_url,
            "provider_context_limit_tokens": CONTROLLED_CONTEXT_LIMIT,
            "compaction_reserve_tokens": 8192,
            "compaction_keep_recent_tokens": 8000,
            "run_timeout_seconds": args.run_timeout_seconds,
        },
        "run_id": run_id,
        "final_answer": outcome.answer,
        "analysis": analysis,
        "checks": checks,
        "verification": {"visible": visible, "hidden": hidden},
        "changed_paths": changed_paths,
        "passed": passed,
    }
    args.patch.parent.mkdir(parents=True, exist_ok=True)
    args.patch.write_text(patch_text.rstrip() + "\n", encoding="utf-8")
    args.artifact.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
