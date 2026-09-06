#!/usr/bin/env python3
"""Run small, repeatable real-LLM evaluations for Pico's core harness paths."""

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

from pico import (
    FakeModelClient,
    OpenAICompatibleModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    ToolOutcome,
    Workspace,
)
from pico.command_runner import CommandRunner, shell_argv
from pico.config import load_project_env, provider_env
from pico.mutations import file_revision
from pico.providers.clients import DEFAULT_OPENAI_BASE_URL
from pico.run_lifecycle import RunLifecycle
from scripts.real_case_support import git_metadata, require_clean_runtime

PYTHON = shlex.quote(sys.executable)
ASK_TOOLS = {
    "list_files",
    "read_artifact",
    "read_file",
    "search",
    "submit_final",
    "update_working_state",
}
ARTIFACT_PATHS = {
    "ask": ROOT / "artifacts" / "real-ask.json",
    "approval": ROOT / "artifacts" / "real-code-approval.json",
    "revision": ROOT / "artifacts" / "real-revision-repair.json",
    "resume": ROOT / "artifacts" / "real-resume.json",
}
PATCH_PATHS = {
    "revision": ROOT / "artifacts" / "real-revision-repair.patch",
    "resume": ROOT / "artifacts" / "real-resume.patch",
}


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


def prepare_workspace(workspace, files):
    """Create a fresh committed repository containing only ``files``."""

    workspace = Path(workspace).resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    (workspace / ".gitignore").write_text(".pico/\n", encoding="utf-8")
    for logical, content in files.items():
        path = workspace / logical
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(workspace, "init", "--quiet")
    _git(workspace, "config", "user.name", "Pico Harness Evaluation")
    _git(workspace, "config", "user.email", "pico-eval@example.invalid")
    _git(workspace, "add", "--all")
    _git(workspace, "commit", "--quiet", "-m", "controlled baseline")
    return workspace


def build_prompt(case):
    prompts = {
        "ask": (
            "Inspect README.md using repository tools and answer with the single project "
            "codename it declares. Do not infer it from this request; read the file first, "
            "then call submit_final."
        ),
        "approval": (
            "Create requested.txt with the exact content `approved only\\n` using write_file. "
            "Do not use run_command. If approval is denied, do not retry the write: inspect "
            "the workspace with one observation tool and submit a concise explanation."
        ),
        "revision": (
            "Read subject.txt, replace the line `alpha` with `agent`, and preserve every "
            "other line. A concurrent edit may cause a revision conflict; if it does, read "
            "the file again and retry with its current revision. Do not use run_command. "
            "Submit final after the edit; the Runtime owns verification."
        ),
        "resume": (
            "Continue the interrupted task. Inspect recovery.txt before acting, replace "
            "`partial` with `recovered`, preserve other content, and do not repeat the "
            "interrupted edit blindly. Do not use run_command. Submit final after repair; "
            "the Runtime owns verification."
        ),
    }
    return prompts[case]


def _command(workspace, command):
    result = CommandRunner(workspace).run(
        shell_argv(command), cwd=workspace, timeout=120, env={}
    )
    return {
        "ok": result.returncode == 0 and not result.stop_reason,
        "infrastructure_error": result.infrastructure_error,
        "exit_code": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
        "stop_reason": result.stop_reason,
    }


def _requested_calls(events):
    calls = []
    for event in events:
        if event.kind == "assistant_tool_call":
            calls.append(
                {"call_id": event.call_id, "name": event.name, "args": event.args}
            )
        elif event.kind == "assistant_tool_batch":
            calls.extend(
                {"call_id": call.call_id, "name": call.name, "args": call.args}
                for call in event.batch_calls
            )
    return calls


def _tool_results(events):
    rows = []
    for event in events:
        if event.kind != "tool_result":
            continue
        outcome = ToolOutcome.from_dict(event.payload["outcome"])
        rows.append(
            {
                "call_id": outcome.tool_call_id,
                "name": outcome.tool_name,
                "status": outcome.status,
                "execution_state": outcome.execution_state,
                "side_effect_state": outcome.side_effect_state,
                "failure": outcome.failure.code if outcome.failure else "",
                "affected_paths": list(outcome.affected_paths),
                "recovered_from_interruption": bool(
                    event.payload.get("recovered_from_interruption")
                ),
            }
        )
    return rows


def _event_analysis(agent, outcome):
    events = agent.dependencies.run_store.read_events(outcome.run_id)
    return events, {
        "event_kinds": [event.kind for event in events],
        "model_request_count": outcome.metrics["model_request_count"],
        "executed_tool_count": outcome.metrics["executed_tool_count"],
        "requested_calls": _requested_calls(events),
        "tool_results": _tool_results(events),
    }


def _client(args):
    return OpenAICompatibleModelClient(
        args.model,
        args.base_url,
        args.api_key,
        args.temperature,
        args.timeout,
    )


def _agent(client, workspace, *, mode, allowed_tools, allowed_paths=(), verifier="", session=None, run_store=None):
    runtime_workspace = Workspace.build(workspace)
    return Pico(
        model_client=client,
        workspace=runtime_workspace,
        run_store=run_store,
        config=PicoConfig(
            mode=mode,
            allowed_tools=tuple(allowed_tools),
            allowed_write_paths=tuple(allowed_paths),
            max_tool_executions=12,
            max_agent_turns=12,
            max_new_tokens=1024,
            turn_timeout_seconds=600,
            verification_command=verifier,
        ),
        command_runner=CommandRunner(workspace),
        session=session
        if session is not None
        else SessionStore(workspace / ".pico" / "sessions").create(
            runtime_workspace.root
        ),
    )


def _artifact(case, args, runtime, outcome, analysis, checks, **extra):
    return {
        "artifact_type": f"pico-real-{case}",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "run_id": outcome.run_id,
        "outcome": outcome.to_dict(),
        "analysis": analysis,
        "checks": checks,
        "passed": all(checks.values()),
        **extra,
    }


def run_ask(args, runtime, workspace):
    workspace = prepare_workspace(workspace, {"README.md": "Project codename: ORBIT-7\n"})
    agent = _agent(
        _client(args),
        workspace,
        mode="ask",
        allowed_tools=tuple(sorted(ASK_TOOLS - {"submit_final"})),
    )
    surface = {tool["name"] for tool in agent.tools.model_action_tools()}
    outcome = agent.ask(build_prompt("ask"))
    _events, analysis = _event_analysis(agent, outcome)
    calls = analysis["requested_calls"]
    changed_paths = _git(workspace, "diff", "--name-only", "HEAD").splitlines()
    checks = {
        "completed": outcome.status == "completed",
        "ask_surface_exact": surface == ASK_TOOLS,
        "observation_used": any(
            call["name"] in {"list_files", "read_file", "search"} for call in calls
        ),
        "readme_read": any(
            call["name"] == "read_file" and call["args"].get("path") == "README.md"
            for call in calls
        ),
        "no_workspace_change": not changed_paths and not outcome.changed_paths,
        "bounded_turns": analysis["model_request_count"] <= 6,
    }
    return _artifact(
        "ask", args, runtime, outcome, analysis, checks,
        tool_surface=sorted(surface), changed_paths=changed_paths,
    )


def run_approval(args, runtime, workspace):
    workspace = prepare_workspace(workspace, {"README.md": "Approval test workspace.\n"})
    approvals = []
    agent = _agent(
        _client(args),
        workspace,
        mode="code",
        allowed_tools=("list_files", "read_file", "write_file", "update_working_state"),
        allowed_paths=("requested.txt",),
    )

    def deny(name, tool_args):
        approvals.append({"name": name, "args": dict(tool_args)})
        return False

    agent.tools.approve = deny
    outcome = agent.ask(build_prompt("approval"))
    events, analysis = _event_analysis(agent, outcome)
    denied = [
        row
        for row in analysis["tool_results"]
        if row["failure"] == "approval_denied"
    ]
    denied_ids = {row["call_id"] for row in denied}
    started_ids = {
        event.call_id for event in events if event.kind == "tool_started"
    }
    calls = analysis["requested_calls"]
    checks = {
        "completed": outcome.status == "completed",
        "one_write_requested": sum(call["name"] == "write_file" for call in calls) == 1,
        "one_approval_denied": len(denied) == 1 and len(approvals) == 1,
        "denied_call_not_started": bool(denied_ids) and denied_ids.isdisjoint(started_ids),
        "denied_outcome_is_effect_free": bool(denied)
        and denied[0]["execution_state"] == "not_started"
        and denied[0]["side_effect_state"] == "none",
        "observation_after_denial": any(
            call["name"] in {"list_files", "read_file", "search"} for call in calls[1:]
        ),
        "file_not_created": not (workspace / "requested.txt").exists(),
        "bounded_turns": analysis["model_request_count"] <= 6,
    }
    return _artifact(
        "code-approval", args, runtime, outcome, analysis, checks,
        approval_requests=approvals,
        changed_paths=_git(workspace, "diff", "--name-only", "HEAD").splitlines(),
    )


class DriftAfterReadClient(OpenAICompatibleModelClient):
    """Inject one real concurrent edit between a model read and its next turn."""

    def __init__(self, *args, target, **kwargs):
        super().__init__(*args, **kwargs)
        self.target = Path(target)
        self._inject_before_next_request = False
        self.drift_injected = False

    def complete_action(self, *args, **kwargs):
        if self._inject_before_next_request and not self.drift_injected:
            self.target.write_text("alpha\nexternal\n", encoding="utf-8")
            self.drift_injected = True
        action = super().complete_action(*args, **kwargs)
        if any(
            call.name == "read_file" and call.args.get("path") == "subject.txt"
            for call in action.tool_calls
        ) and not self.drift_injected:
            self._inject_before_next_request = True
        return action


def run_revision(args, runtime, workspace):
    workspace = prepare_workspace(workspace, {"subject.txt": "alpha\n"})
    target = workspace / "subject.txt"
    verifier = (
        f"{PYTHON} -c \"from pathlib import Path; "
        "assert Path('subject.txt').read_text() == 'agent\\nexternal\\n'\""
    )
    client = DriftAfterReadClient(
        args.model,
        args.base_url,
        args.api_key,
        args.temperature,
        args.timeout,
        target=target,
    )
    agent = _agent(
        client,
        workspace,
        mode="auto",
        allowed_tools=("read_file", "edit_file", "update_working_state"),
        allowed_paths=("subject.txt",),
        verifier=verifier,
    )
    outcome = agent.ask(build_prompt("revision"))
    _events, analysis = _event_analysis(agent, outcome)
    results = analysis["tool_results"]
    calls = analysis["requested_calls"]
    conflicts = [row for row in results if row["failure"] == "revision_conflict"]
    successes = [
        row
        for row in results
        if row["name"] == "edit_file"
        and row["status"] == "success"
        and row["side_effect_state"] == "changed"
    ]
    verification = _command(workspace, verifier)
    patch = _git(workspace, "diff", "--binary", "--unified=1", "HEAD")
    checks = {
        "completed": outcome.status == "completed",
        "drift_injected": client.drift_injected,
        "conflict_observed": len(conflicts) == 1,
        "reread_after_conflict": sum(
            call["name"] == "read_file" and call["args"].get("path") == "subject.txt"
            for call in calls
        ) >= 2,
        "one_successful_repair": len(successes) == 1,
        "concurrent_content_preserved": target.read_text(encoding="utf-8")
        == "agent\nexternal\n",
        "runtime_verifier_passed": analysis["event_kinds"].count("verification_result") >= 1,
        "independent_verifier_passed": verification["ok"],
        "scope_valid": list(outcome.changed_paths) == ["subject.txt"],
        "bounded_turns": analysis["model_request_count"] <= 8,
    }
    return _artifact(
        "revision-repair", args, runtime, outcome, analysis, checks,
        verification=verification,
        changed_paths=_git(workspace, "diff", "--name-only", "HEAD").splitlines(),
    ), patch


def run_resume(args, runtime, workspace):
    workspace = prepare_workspace(workspace, {"recovery.txt": "baseline\n"})
    target = workspace / "recovery.txt"
    verifier = (
        f"{PYTHON} -c \"from pathlib import Path; "
        "assert Path('recovery.txt').read_text() == 'recovered\\n'\""
    )
    session_store = SessionStore(workspace / ".pico" / "sessions")
    original = _agent(
        FakeModelClient([]),
        workspace,
        mode="auto",
        allowed_tools=("read_file", "edit_file", "update_working_state"),
        allowed_paths=("recovery.txt",),
        verifier=verifier,
    )
    RunLifecycle(original).initialize("Replace baseline with recovered in recovery.txt")
    run_id = original.run.projection.run_id
    call = ToolCall(
        "edit_file",
        {
            "path": "recovery.txt",
            "old_text": "baseline\n",
            "new_text": "partial\n",
            "expected_revision": file_revision(target),
        },
        "call_interrupted_edit",
    )
    preimage = original.dependencies.artifacts.write_workspace_preimage(
        run_id, call.call_id, "recovery.txt", target
    )
    original.run.run_log.append_tool_call(call)
    original.run.run_log.append_tool_started(
            call,
            effect_scope="workspace",
            potential_effects=[
                {
                    "path": "recovery.txt",
                    "before_state": file_revision(target),
                    "before_artifact_id": preimage["artifact_id"],
                }
            ],
        )
    target.write_text("partial\n", encoding="utf-8")
    original.run.execution_context = None

    session = session_store.load(original.session.id)
    resumed = _agent(
        _client(args),
        workspace,
        mode="auto",
        allowed_tools=("read_file", "edit_file", "update_working_state"),
        allowed_paths=("recovery.txt",),
        verifier=verifier,
        session=session,
        run_store=original.dependencies.run_store,
    )
    dormant = {
        "same_run_id": resumed.run.projection.run_id == run_id,
        "resumable": resumed.run.resumable,
        "pending_call_id": resumed.run.projection.pending_call_id,
    }
    outcome = resumed.ask(build_prompt("resume"))
    events, analysis = _event_analysis(resumed, outcome)
    recovered = [
        row
        for row in analysis["tool_results"]
        if row["call_id"] == call.call_id and row["recovered_from_interruption"]
    ]
    original_starts = [
        event
        for event in events
        if event.kind == "tool_started" and event.call_id == call.call_id
    ]
    calls = analysis["requested_calls"]
    verification = _command(workspace, verifier)
    patch = _git(workspace, "diff", "--binary", "--unified=1", "HEAD")
    checks = {
        "dormant_run_restored": dormant
        == {"same_run_id": True, "resumable": True, "pending_call_id": call.call_id},
        "same_run_completed": outcome.run_id == run_id and outcome.status == "completed",
        "interruption_classified_partial": len(recovered) == 1
        and recovered[0]["status"] == "partial_success"
        and recovered[0]["side_effect_state"] == "partial",
        "interrupted_call_not_replayed": len(original_starts) == 1
        and sum(item["call_id"] == call.call_id for item in calls) == 1,
        "state_observed_before_repair": any(
            item["name"] == "read_file" and item["args"].get("path") == "recovery.txt"
            for item in calls[1:]
        ),
        "one_successful_repair": sum(
            row["name"] == "edit_file"
            and row["status"] == "success"
            and row["side_effect_state"] == "changed"
            for row in analysis["tool_results"]
        ) == 1,
        "final_content_recovered": target.read_text(encoding="utf-8") == "recovered\n",
        "runtime_verifier_passed": analysis["event_kinds"].count("verification_result") >= 1,
        "independent_verifier_passed": verification["ok"],
        "scope_valid": list(outcome.changed_paths) == ["recovery.txt"],
        "bounded_turns": analysis["model_request_count"] <= 8,
    }
    return _artifact(
        "resume", args, runtime, outcome, analysis, checks,
        dormant_snapshot=dormant,
        verification=verification,
        changed_paths=_git(workspace, "diff", "--name-only", "HEAD").splitlines(),
    ), patch


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=(*ARTIFACT_PATHS, "all"), default="all")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=ROOT / "artifacts" / "real-harness-workspaces",
    )
    args = parser.parse_args(argv)

    runtime = git_metadata()
    require_clean_runtime(runtime)
    load_project_env(ROOT, boundary=ROOT)
    args.api_key = provider_env("PICO_OPENAI_API_KEY")
    if not args.api_key:
        raise RuntimeError("PICO_OPENAI_API_KEY is required")
    args.base_url = args.base_url or provider_env(
        "PICO_OPENAI_API_BASE", DEFAULT_OPENAI_BASE_URL
    )

    selected = tuple(ARTIFACT_PATHS) if args.case == "all" else (args.case,)
    results = []
    for case in selected:
        workspace = args.workspace_root / case
        if case == "ask":
            result = run_ask(args, runtime, workspace)
            patch = None
        elif case == "approval":
            result = run_approval(args, runtime, workspace)
            patch = None
        elif case == "revision":
            result, patch = run_revision(args, runtime, workspace)
        else:
            result, patch = run_resume(args, runtime, workspace)
        path = ARTIFACT_PATHS[case]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if patch is not None:
            PATCH_PATHS[case].write_text(patch.rstrip() + "\n", encoding="utf-8")
        results.append({"case": case, "passed": result["passed"], "checks": result["checks"]})

    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
