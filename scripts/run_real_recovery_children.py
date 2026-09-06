#!/usr/bin/env python3
"""Real-model regressions for accepting recovered edits and sequential children.

Only the interruption is injected by the harness. Every model decision, child,
workspace mutation, integration, and verification uses the production Runtime.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico import Pico, PicoConfig, SessionStore, ToolCall, Workspace
from pico.command_runner import CommandRunner
from pico.config import load_project_env, provider_env
from pico.mutations import file_revision
from pico.providers.clients import DEFAULT_OPENAI_BASE_URL
from pico.run_lifecycle import RunLifecycle
from scripts.real_case_support import git_metadata, require_clean_runtime
from scripts.run_real_harness_cases import (
    _agent,
    _artifact,
    _client,
    _command,
    _event_analysis,
    _git,
    prepare_workspace,
)


def python_check(code):
    return shlex.join([sys.executable, "-B", "-c", code])


def run_resume_accepted(args, runtime, workspace):
    workspace = prepare_workspace(workspace, {"recovery.txt": "baseline\n"})
    target = workspace / "recovery.txt"
    verifier = python_check(
        "from pathlib import Path; "
        "assert Path('recovery.txt').read_text() == 'recovered\\n'"
    )
    options = {
        "mode": "auto", "allowed_tools": ("read_file", "edit_file", "update_working_state"),
        "allowed_paths": ("recovery.txt",), "verifier": verifier,
    }
    original = _agent(_client(args), workspace, **options)
    RunLifecycle(original).initialize("Replace baseline with recovered in recovery.txt")
    run_id = original.run.projection.run_id
    call = ToolCall("edit_file", {
        "path": "recovery.txt", "old_text": "baseline\n", "new_text": "recovered\n",
        "expected_revision": file_revision(target),
    }, "call_interrupted_correct_edit")
    preimage = original.dependencies.artifacts.write_workspace_preimage(
        run_id, call.call_id, "recovery.txt", target
    )
    original.run.run_log.append_tool_call(call)
    original.run.run_log.append_tool_started(call, effect_scope="workspace", potential_effects=[{
        "path": "recovery.txt", "before_state": file_revision(target),
        "before_artifact_id": preimage["artifact_id"],
    }])
    # Emulate process loss after the intended bytes reached disk, before a receipt.
    target.write_text("recovered\n", encoding="utf-8")
    original.run.execution_context = None
    session = SessionStore(workspace / ".pico" / "sessions").load(original.session.id)
    resumed = _agent(_client(args), workspace, session=session,
                     run_store=original.dependencies.run_store, **options)
    outcome = resumed.ask(
        "Continue the interrupted task. Read recovery.txt to inspect its current state. "
        "The required exact content is recovered followed by a newline. If already "
        "correct, leave the file as it is and submit_final; Runtime runs verification. "
        "Do not manufacture an edit to acknowledge recovery."
    )
    events, analysis = _event_analysis(resumed, outcome)
    calls = analysis["requested_calls"]
    recovered = [row for row in analysis["tool_results"] if row["call_id"] == call.call_id
                 and row["recovered_from_interruption"]]
    verification = _command(workspace, verifier)
    checks = {
        "same_run_completed": outcome.run_id == run_id and outcome.status == "completed",
        "partial_receipt_recovered": len(recovered) == 1
        and recovered[0]["side_effect_state"] == "partial",
        "original_call_not_replayed": sum(event.kind == "tool_started"
                                          and event.call_id == call.call_id for event in events) == 1,
        "current_file_read": any(item["name"] == "read_file"
                                 and item["args"].get("path") == "recovery.txt" for item in calls[1:]),
        "no_manufactured_mutation": all(item["name"] not in {"edit_file", "write_file"}
                                        for item in calls[1:]),
        "runtime_verification_passed": any(event.kind == "verification_result"
                                            and event.payload.get("status") == "passed" for event in events),
        "independent_verification_passed": verification["ok"],
        "scope_valid": list(outcome.changed_paths) == ["recovery.txt"],
    }
    return _artifact("resume-accepted", args, runtime, outcome, analysis, checks,
                     verification=verification), _git(workspace, "diff", "--binary", "HEAD")


def run_sequential_children(args, runtime, workspace):
    source = "def a():\n    return 0\n" + "\n" * 20 + "def b():\n    return 0\n"
    workspace = prepare_workspace(workspace, {"common.py": source})
    head_before = _git(workspace, "rev-parse", "HEAD")
    # Both intermediate deliveries are valid. Final task correctness is separately
    # checked below, after both real children have delivered their changes.
    verifier = python_check(
        "from common import a,b; assert a() in (0,1); assert b() in (0,2)"
    )
    runtime_workspace = Workspace.build(workspace)
    parent = Pico(
        model_client=_client(args), workspace=runtime_workspace,
        config=PicoConfig(
            mode="auto", allowed_tools=("read_file", "delegate", "integrate_child", "update_working_state"),
            allowed_write_paths=("common.py",), verification_command=verifier,
            max_tool_executions=20, max_agent_turns=20, turn_timeout_seconds=900,
        ),
        command_runner=CommandRunner(workspace),
        session=SessionStore(workspace / ".pico" / "sessions").create(runtime_workspace.root),
        subagent_model_client_factory=lambda _spec: _client(args),
    )
    outcome = parent.ask(
        "Deliver two sequential changes to common.py using exactly two implement children. "
        "First delegate a child with common.py as its only allowed write path: change a() "
        "to return 1, preserve b(). Integrate that child's patch before delegating the "
        "second child. Then delegate another implement child with the same only write path: "
        "change b() to return 2 and preserve the delivered a() returning 1. Integrate its "
        "patch. Read the final file to confirm both changes and submit_final. "
        "Do not edit directly. Runtime verifies each valid intermediate delivery."
    )
    events, analysis = _event_analysis(parent, outcome)
    results = analysis["tool_results"]
    deliveries = [row for row in results if row["name"] in {"delegate", "integrate_child"}]
    verification = _command(workspace, python_check(
        "from common import a,b; assert a() == 1; assert b() == 2"
    ))
    records = list(parent.run.projection.children.records.values())
    child_runs = []
    for record in records:
        if record.result is not None and record.result.child_run_id:
            store = parent.dependencies.subagents._child_run_store(outcome.run_id, record)
            child_events = store.read_events(record.result.child_run_id)
            child_runs.append({
                "child_id": record.child_id,
                "run_id": record.result.child_run_id,
                "model_requests": sum(e.kind == "model_requested" for e in child_events),
                "verified": any(e.kind == "verification_result"
                                and e.payload.get("status") == "passed" for e in child_events),
            })
    checks = {
        "parent_completed": outcome.status == "completed",
        "sequential_delivery_order": [row["name"] for row in deliveries]
        == ["delegate", "integrate_child", "delegate", "integrate_child"],
        "all_deliveries_succeeded": len(deliveries) == 4
        and all(row["status"] == "success" for row in deliveries),
        "two_verified_children": len(child_runs) == 2 and all(row["verified"] for row in child_runs),
        "no_parent_direct_edits": not any(row["name"] in {"edit_file", "write_file"}
                                         for row in analysis["requested_calls"]),
        "independent_combined_verification_passed": verification["ok"],
        "scope_valid": list(outcome.changed_paths) == ["common.py"],
        "parent_head_preserved": _git(workspace, "rev-parse", "HEAD") == head_before,
        "parent_index_preserved": _git(workspace, "diff", "--cached") == "",
        "runtime_verification_passed": any(e.kind == "verification_result"
                                            and e.payload.get("status") == "passed" for e in events),
    }
    return _artifact("sequential-children", args, runtime, outcome, analysis, checks,
                     verification=verification, child_runs=child_runs), _git(workspace, "diff", "--binary", "HEAD")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("resume-accepted", "sequential-children"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    runtime = git_metadata()
    require_clean_runtime(runtime)
    load_project_env(ROOT, boundary=ROOT)
    args.api_key = provider_env("PICO_OPENAI_API_KEY")
    if not args.api_key:
        raise RuntimeError("PICO_OPENAI_API_KEY is required")
    args.base_url = args.base_url or provider_env("PICO_OPENAI_API_BASE", DEFAULT_OPENAI_BASE_URL)
    args.output.mkdir(parents=True, exist_ok=True)
    runner = run_resume_accepted if args.case == "resume-accepted" else run_sequential_children
    report, patch = runner(args, runtime, args.output / "workspaces" / args.case)
    (args.output / f"{args.case}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (args.output / f"{args.case}.patch").write_text(patch + "\n")
    print(json.dumps({"case": args.case, "passed": report["passed"], "checks": report["checks"]}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
