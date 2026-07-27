"""Runtime-verifier execution and freshness rules."""

import hashlib
import json
import time

from . import security, workspace_diff
from .config import DEFAULT_RUNTIME_VERIFICATION_TIMEOUT_SECONDS


def workspace_fingerprint(agent):
    """Return the content fingerprint used to bind verifier evidence."""
    snapshot = workspace_diff.capture_workspace_snapshot(agent)
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tool_invalidates_runtime_verification(name, metadata):
    if bool(metadata.get("workspace_changed")):
        return True
    status = str(metadata.get("tool_status", "")).strip()
    if status != "ok":
        return False
    return name in {"write_file", "patch_file", "run_shell"}


def invalidate_runtime_verifications(agent, name, metadata, *, tool_step):
    current_records = [
        record
        for record in agent.runtime_verifications
        if str(record.get("freshness", "")) == "current"
    ]
    if not current_records or not _tool_invalidates_runtime_verification(name, metadata):
        return 0

    current_fingerprint = workspace_fingerprint(agent)
    for record in current_records:
        record["freshness"] = "stale"
        record["invalidated_by"] = {
            "kind": "tool",
            "tool": str(name),
            "tool_step": int(tool_step),
            "workspace_fingerprint": current_fingerprint,
        }
    return len(current_records)


def runtime_verification_is_current(agent, record):
    if str(record.get("status", "")) != "passed":
        return False
    if str(record.get("freshness", "")) != "current":
        return False

    current_fingerprint = workspace_fingerprint(agent)
    if str(record.get("workspace_fingerprint", "")) == current_fingerprint:
        return True

    record["freshness"] = "stale"
    record["invalidated_by"] = {
        "kind": "workspace_fingerprint_mismatch",
        "workspace_fingerprint": current_fingerprint,
    }
    return False


def _verification_output(stdout, stderr, *, limit=3200):
    output = "\n".join(
        part for part in (str(stdout or "").strip(), str(stderr or "").strip()) if part
    )
    if len(output) <= limit:
        return output
    return f"...[runtime verification output truncated]...\n{output[-limit:]}"


def run_runtime_verification(agent, task_state):
    """Run the user-configured verifier outside the model tool path."""
    command = str(agent.workspace.verification_command or "").strip()
    started = time.monotonic()
    record = {
        "command": command,
        "status": "infrastructure_error",
        "passed": False,
        "freshness": "current",
        "workspace_fingerprint": "",
        "invalidated_by": {},
        "exit_code": None,
        "timed_out": False,
        "duration_ms": 0,
        "output": "",
    }
    try:
        result = agent.sandbox.run(
            command,
            cwd=agent.root,
            timeout=DEFAULT_RUNTIME_VERIFICATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        record["output"] = security.redact_text(
            agent, f"runtime verifier could not start: {type(exc).__name__}: {exc}"
        )
    else:
        raw_output = _verification_output(result.stdout, result.stderr)
        output = security.redact_text(agent, raw_output)
        passed = result.returncode == 0
        record.update(
            {
                "status": "passed" if passed else "failed",
                "passed": passed,
                "exit_code": int(result.returncode),
                "timed_out": bool(result.timed_out),
                "output": output,
            }
        )
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    record["workspace_fingerprint"] = workspace_fingerprint(agent)
    agent.runtime_verifications.append(record)
    agent.emit_trace(
        task_state,
        "verifier_end",
        {
            "verifier": "runtime",
            "status": record["status"],
            "passed": record["passed"],
            "freshness": record["freshness"],
            "workspace_fingerprint": record["workspace_fingerprint"],
            "exit_code": record["exit_code"],
            "timed_out": record["timed_out"],
            "duration_ms": record["duration_ms"],
            "output_chars": len(record["output"]),
        },
    )
    return record


def verification_feedback(record):
    command = str(record.get("command", ""))
    output = str(record.get("output", "")).strip() or "(no verifier output)"
    return (
        "Runtime verification failed. You have exactly one repair attempt. "
        "Inspect the failure, make the smallest correct fix, then call submit_final again.\n"
        f"Command: {command}\n"
        f"Exit code: {record.get('exit_code')}\n"
        f"Output:\n{output}"
    )


def verified_change_final(agent, progress_tracker):
    changed_paths = sorted(
        {
            str(path)
            for entry in agent.tool_audit_log
            for path in entry.get("affected_paths", [])
            if str(path).strip()
        }
    )
    verification = dict(progress_tracker.get("last_runtime_verification") or {})
    command = str(verification.get("command", "verification command")).strip()
    changed = ", ".join(changed_paths) if changed_paths else "workspace changes"
    return f"Completed verified changes.\nChanged files: {changed}\nRuntime verification: {command} — passed."
