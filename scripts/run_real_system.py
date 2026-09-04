#!/usr/bin/env python3
"""Run Pico through its real CLI on a small multi-file coding task."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pico import ToolOutcome
from pico.command_runner import CommandRunner, shell_argv
from pico.config import load_project_env, provider_env
from pico.providers.clients import DEFAULT_OPENAI_BASE_URL
from pico.run_store import RunStore
from scripts.real_case_support import git_metadata, require_clean_runtime
from scripts.run_real_harness_cases import prepare_workspace

TARGET_PATH = "inventory/pricing.py"
PYTHON = shlex.quote(sys.executable)
VISIBLE_COMMAND = f"{PYTHON} -m pytest -q"
HIDDEN_COMMAND = (
    f"{PYTHON} -c \"from inventory.models import LineItem; "
    "from inventory.pricing import order_total; "
    "assert order_total([LineItem('bulk', 125, 8)]) == 1000; "
    "assert order_total([]) == 0\""
)


def system_files():
    return {
        "AGENTS.md": (
            "# Project instructions\n\n"
            "- Keep monetary values as integer cents.\n"
            "- Never modify test files.\n"
            "- Include `AGENTS-FOLLOWED` in the completion summary.\n"
        ),
        "README.md": (
            "# Pocket Inventory\n\n"
            "A tiny order-pricing package. Monetary values are integer cents.\n"
        ),
        "inventory/__init__.py": (
            "from .models import LineItem\n"
            "from .pricing import order_total\n\n"
            "__all__ = ['LineItem', 'order_total']\n"
        ),
        "inventory/models.py": (
            "from dataclasses import dataclass\n\n\n"
            "@dataclass(frozen=True)\n"
            "class LineItem:\n"
            "    sku: str\n"
            "    unit_price: int\n"
            "    quantity: int\n"
        ),
        TARGET_PATH: (
            "from .models import LineItem\n\n\n"
            "def order_total(items: list[LineItem]) -> int:\n"
            "    return sum(item.unit_price for item in items)\n"
        ),
        "inventory/report.py": (
            "from .models import LineItem\n"
            "from .pricing import order_total\n\n\n"
            "def render_total(items: list[LineItem]) -> str:\n"
            "    return f'Total: {order_total(items)} cents'\n"
        ),
        "tests/test_pricing.py": (
            "from inventory.models import LineItem\n"
            "from inventory.pricing import order_total\n\n\n"
            "def test_total_uses_quantity():\n"
            "    items = [LineItem('pen', 150, 2), LineItem('book', 700, 3)]\n"
            "    assert order_total(items) == 2400\n"
        ),
        "tests/test_report.py": (
            "from inventory.models import LineItem\n"
            "from inventory.report import render_total\n\n\n"
            "def test_render_total():\n"
            "    assert render_total([LineItem('pen', 150, 2)]) == 'Total: 300 cents'\n"
        ),
    }


def build_prompt(*, delegate=False):
    workflow = (
        "Inspect the implementation, then delegate exactly one implement Child to fix "
        "inventory/pricing.py with that single allowed write path. Integrate the returned "
        "patch using integrate_child; do not edit files directly. "
        if delegate
        else "Solve this small task directly without delegation. "
    )
    return (
        "Customers report that order totals are too low whenever a line item has a "
        "quantity greater than one. Diagnose and fix the root cause. Preserve the public "
        "API and do not modify tests. "
        + workflow
        +
        "Repository instructions are already supplied by the Runtime, so do not open "
        "AGENTS.md. Use repository tools to locate the implementation; the Runtime owns "
        "verification."
    )


def _git(root, *args):
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def _run_command(workspace, command):
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
            calls.append({"name": event.name, "args": event.args})
        elif event.kind == "assistant_tool_batch":
            calls.extend({"name": call.name, "args": call.args} for call in event.batch_calls)
    return calls


def _tool_results(events):
    rows = []
    for event in events:
        if event.kind != "tool_result":
            continue
        outcome = ToolOutcome.from_dict(event.payload["outcome"])
        rows.append(
            {
                "name": outcome.tool_name,
                "status": outcome.status,
                "execution_state": outcome.execution_state,
                "side_effect_state": outcome.side_effect_state,
                "failure": outcome.failure.code if outcome.failure else "",
                "affected_paths": list(outcome.affected_paths),
            }
        )
    return rows


def _single_run(workspace):
    root = workspace / ".pico" / "runs"
    run_ids = sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "events.jsonl").is_file()
    )
    if len(run_ids) != 1:
        raise RuntimeError(f"expected one top-level Run, found {len(run_ids)}")
    store = RunStore(root)
    events, projection = store.load_run(run_ids[0])
    return run_ids[0], events, projection


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--base-url")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--turn-timeout", type=int, default=600)
    parser.add_argument("--delegate", action="store_true")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--patch",
        type=Path,
        default=None,
    )
    args = parser.parse_args(argv)
    artifact_name = "real-child" if args.delegate else "real-system"
    args.workspace = args.workspace or ROOT / "artifacts" / f"{artifact_name}-workspace"
    args.artifact = args.artifact or ROOT / "artifacts" / f"{artifact_name}.json"
    args.patch = args.patch or ROOT / "artifacts" / f"{artifact_name}.patch"

    runtime = git_metadata()
    require_clean_runtime(runtime)
    load_project_env(ROOT, boundary=ROOT)
    api_key = provider_env("PICO_OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("PICO_OPENAI_API_KEY is required")
    base_url = args.base_url or provider_env(
        "PICO_OPENAI_API_BASE", DEFAULT_OPENAI_BASE_URL
    )
    workspace = prepare_workspace(args.workspace, system_files())
    initial = _run_command(workspace, VISIBLE_COMMAND)
    if initial["infrastructure_error"] or initial["ok"]:
        raise RuntimeError("controlled system baseline must execute and fail")

    command = [
        sys.executable,
        "-m",
        "pico",
        "--cwd",
        str(workspace),
        "--mode",
        "auto",
        "--model",
        args.model,
        "--base-url",
        base_url,
        "--temperature",
        str(args.temperature),
        "--openai-timeout",
        str(args.timeout),
        "--turn-timeout",
        str(args.turn_timeout),
        "--max-agent-turns",
        "16",
        "--max-tool-executions",
        "20",
        build_prompt(delegate=args.delegate),
    ]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    cli = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=args.turn_timeout + 30,
    )

    run_id, events, projection = _single_run(workspace)
    calls = _requested_calls(events)
    results = _tool_results(events)
    changed_paths = _git(workspace, "diff", "--name-only", "HEAD").splitlines()
    patch_text = _git(workspace, "diff", "--binary", "--unified=1", "HEAD")
    visible = _run_command(workspace, VISIBLE_COMMAND)
    hidden = _run_command(workspace, HIDDEN_COMMAND)
    successful_mutations = [
        row
        for row in results
        if row["name"] in {"write_file", "edit_file", "integrate_child"}
        and row["status"] == "success"
        and row["side_effect_state"] == "changed"
    ]
    read_paths = [
        str(call["args"].get("path", ""))
        for call in calls
        if call["name"] == "read_file"
    ]
    checks = {
        "baseline_failure_reproduced": not initial["ok"],
        "cli_completed": cli.returncode == 0,
        "one_terminal_run": projection.status == "completed",
        "implementation_read": TARGET_PATH in read_paths,
        "bounded_successful_mutations": 1 <= len(successful_mutations) <= 3,
        "runtime_verification_passed": any(
            event.kind == "verification_result"
            and event.payload.get("status") == "passed"
            for event in events
        ),
        "repository_instruction_followed_without_file_read": (
            "AGENTS-FOLLOWED" in projection.final_answer
            and "AGENTS.md" not in read_paths
        ),
        "auto_verifier_selected": "VERIFY" in cli.stdout
        and "pytest -q" in cli.stdout,
        "visible_verifier_passed": visible["ok"],
        "hidden_verifier_passed": hidden["ok"],
        "scope_valid": changed_paths == [TARGET_PATH]
        and list(projection.evidence.changed_paths) == [TARGET_PATH],
        "final_answer_printed": bool(projection.final_answer)
        and projection.final_answer in cli.stdout,
        "bounded_model_turns": projection.model_request_count <= 10,
        "patch_recorded": bool(patch_text),
    }
    child_receipts = [
        event.payload["outcome"]["structured"]
        for event in events
        if event.kind == "tool_result"
        and event.name == "delegate"
        and event.outcome_status == "success"
    ]
    if args.delegate:
        checks["one_child_patch_received"] = (
            len(child_receipts) == 1 and "patch" in child_receipts[0]
        )
        checks["child_patch_integrated"] = sum(
            row["name"] == "integrate_child" and row["status"] == "success"
            for row in results
        ) == 1
        checks["parent_did_not_edit_directly"] = not any(
            call["name"] in {"write_file", "edit_file"} for call in calls
        )
    else:
        checks["target_located_without_prompt_hint"] = (
            TARGET_PATH not in build_prompt() and TARGET_PATH in read_paths
        )
    artifact = {
        "artifact_type": "pico-" + artifact_name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime,
        "model": args.model,
        "provider_base_url": base_url,
        "run_id": run_id,
        "cli": {
            "exit_code": cli.returncode,
            "stdout": cli.stdout[-5000:],
            "stderr": cli.stderr[-5000:],
        },
        "analysis": {
            "model_request_count": projection.model_request_count,
            "executed_tool_count": projection.executed_tool_count,
            "requested_calls": calls,
            "tool_results": results,
            "event_kinds": [event.kind for event in events],
            "child_receipts": child_receipts,
        },
        "verification": {"initial": initial, "visible": visible, "hidden": hidden},
        "changed_paths": changed_paths,
        "checks": checks,
        "passed": all(checks.values()),
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.patch.write_text(patch_text.rstrip() + "\n", encoding="utf-8")
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
