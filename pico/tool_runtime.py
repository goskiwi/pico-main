"""Tool execution, audit, and run-report helpers."""

import re

from . import approval
from . import security
from . import tool_policy
from . import workspace_diff
from .sandbox import SandboxError
from .workspace import clip


def record_process_note_for_tool(agent, name, metadata):
    status = str(metadata.get("tool_status", "")).strip()
    if status not in {"partial_success", "error", "rejected"}:
        return
    affected_paths = [str(path).strip() for path in metadata.get("affected_paths", []) if str(path).strip()]
    path_text = ", ".join(affected_paths) or "workspace"
    if status == "partial_success":
        text = f"{name} partial_success on {path_text}; inspect diff before retry"
    elif status == "error":
        text = f"{name} error on {path_text}; check the failure before retry"
    else:
        text = f"{name} rejected; choose a different action before retry"
    tags = ["process", status, *affected_paths]
    agent.memory.append_note(text, tags=tuple(tags), source=name, kind="process")
    agent.session["memory"] = agent.memory.to_dict()


def _result_metadata(
    agent,
    tool,
    *,
    status,
    error_code="",
    security_event_type="",
    risk_level=None,
    dry_run=None,
    approval_required=False,
    approval_decision="not_required",
    shell_policy=None,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint=None,
    diff_summary=None,
    sandbox_metadata=None,
):
    """Build the stable metadata shape shared by every tool outcome."""
    capability = tool_policy.tool_capability(tool)
    metadata = {
        "tool_status": status,
        "tool_error_code": error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level or tool_policy.tool_risk_level(tool),
        "capability": capability,
        "read_only": capability == "read",
        "dry_run": bool(agent.dry_run if dry_run is None else dry_run),
        "approval_required": bool(approval_required),
        "approval_decision": approval_decision,
        **tool_policy.shell_policy_metadata(shell_policy),
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
    }
    if workspace_fingerprint is not None:
        metadata["workspace_fingerprint"] = workspace_fingerprint
    if sandbox_metadata:
        metadata.update(dict(sandbox_metadata))
    return metadata


def _store_outcome(agent, name, tool, *, record_note=False, **updates):
    metadata = _result_metadata(agent, tool, **updates)
    agent._last_tool_result_metadata = metadata
    if record_note:
        record_process_note_for_tool(agent, name, metadata)
    return metadata


def run_tool(agent, name, args):
    tool = agent.tools.get(name)
    if tool is None:
        _store_outcome(
            agent, name, None, status="rejected", error_code="unknown_tool", risk_level="high"
        )
        return f"error: unknown tool '{name}'"
    try:
        agent.validate_tool(name, args)
    except Exception as exc:
        example = agent.tool_example(name)
        message = f"error: invalid arguments for {name}: {exc}"
        if example:
            message += f"\nexample: {example}"
        security_event_type = ""
        if "path escapes workspace" in str(exc):
            security_event_type = "path_escape"
        elif "dangerous shell command blocked" in str(exc):
            security_event_type = "dangerous_shell_command"
        elif "protected write path blocked" in str(exc):
            security_event_type = "protected_write_path"
        elif "protected read path blocked" in str(exc):
            security_event_type = "protected_read_path"
        _store_outcome(
            agent,
            name,
            tool,
            status="rejected",
            error_code="invalid_arguments",
            security_event_type=security_event_type,
            risk_level="high" if tool["risky"] else "low",
            record_note=True,
        )
        return message
    permission_error = tool_policy.tool_permission_error(agent, tool)
    if permission_error:
        _store_outcome(
            agent,
            name,
            tool,
            status="rejected",
            error_code=permission_error["code"],
            security_event_type=permission_error["security_event_type"],
            approval_decision="denied",
        )
        return permission_error["message"]
    if tool_policy.repeated_tool_call(agent, name, args):
        _store_outcome(
            agent, name, tool, status="rejected", error_code="repeated_identical_call"
        )
        return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
    shell_policy = tool_policy.shell_command_policy(name, args)
    if shell_policy and not shell_policy["allowed"] and agent.approval_policy == "never":
        _store_outcome(
            agent,
            name,
            tool,
            status="rejected",
            error_code="shell_not_allowlisted",
            security_event_type="shell_not_allowlisted",
            approval_required=True,
            approval_decision="denied",
            shell_policy=shell_policy,
        )
        return "error: shell command is not on the allowlist"
    if agent.dry_run and tool["risky"]:
        result = tool_policy.dry_run_tool_result(name, args)
        _store_outcome(
            agent,
            name,
            tool,
            status="dry_run",
            dry_run=True,
            approval_decision="dry_run",
            shell_policy=shell_policy,
            workspace_fingerprint=agent.workspace.fingerprint(),
        )
        return result
    if tool["risky"] and not approval.approve(agent, name, args):
        _store_outcome(
            agent,
            name,
            tool,
            status="rejected",
            error_code="approval_denied",
            security_event_type="read_only_block" if agent.read_only else "approval_denied",
            risk_level="high",
            approval_required=True,
            approval_decision="denied",
            shell_policy=shell_policy,
        )
        return f"error: approval denied for {name}"
    before_snapshot, snapshot_mode = workspace_diff.before_workspace_snapshot(agent, name, args, tool)
    after_snapshot = before_snapshot
    if name == "run_shell":
        agent._last_sandbox_metadata = agent.sandbox.audit_metadata()
    try:
        result = security.redact_text(agent, clip(tool["run"](args)))
        after_snapshot = workspace_diff.after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = workspace_diff.diff_workspace_snapshots(before_snapshot, after_snapshot)
        workspace_changed = bool(affected_paths)
        tool_status = "ok"
        tool_error_code = ""
        security_event_type = ""
        sandbox_metadata = dict(agent._last_sandbox_metadata or {}) if name == "run_shell" else {}
        if name == "run_shell":
            match = re.search(r"exit_code:\s*(-?\d+)", result)
            exit_code = int(match.group(1)) if match else 0
            if exit_code != 0 and workspace_changed:
                tool_status = "partial_success"
                tool_error_code = "tool_partial_success"
            elif exit_code != 0:
                tool_status = "error"
                tool_error_code = "tool_failed"
            if sandbox_metadata.get("sandbox_timed_out"):
                tool_error_code = "sandbox_timeout"
                security_event_type = "sandbox_timeout"
        agent.update_memory_after_tool(name, args, result)
        _store_outcome(
            agent,
            name,
            tool,
            status=tool_status,
            error_code=tool_error_code,
            security_event_type=security_event_type,
            dry_run=False,
            approval_required=bool(tool["risky"]),
            approval_decision="granted" if tool["risky"] else "not_required",
            shell_policy=shell_policy,
            affected_paths=affected_paths,
            workspace_changed=workspace_changed,
            workspace_fingerprint=agent.workspace.fingerprint(),
            diff_summary=diff_summary,
            sandbox_metadata=sandbox_metadata,
            record_note=True,
        )
        return result
    except Exception as exc:
        after_snapshot = workspace_diff.after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = workspace_diff.diff_workspace_snapshots(before_snapshot, after_snapshot)
        workspace_changed = bool(affected_paths)
        security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
        tool_error_code = "tool_partial_success" if workspace_changed else "tool_failed"
        if isinstance(exc, SandboxError):
            security_event_type = exc.security_event_type
            tool_error_code = exc.code
        if "protected write path blocked" in str(exc):
            security_event_type = "protected_write_path"
        sandbox_metadata = dict(agent._last_sandbox_metadata or {}) if name == "run_shell" else {}
        _store_outcome(
            agent,
            name,
            tool,
            status="partial_success" if workspace_changed else "error",
            error_code=tool_error_code,
            security_event_type=security_event_type,
            dry_run=False,
            approval_required=bool(tool["risky"]),
            approval_decision="granted" if tool["risky"] else "not_required",
            shell_policy=shell_policy,
            affected_paths=affected_paths,
            workspace_changed=workspace_changed,
            workspace_fingerprint=agent.workspace.fingerprint(),
            diff_summary=diff_summary,
            sandbox_metadata=sandbox_metadata,
            record_note=True,
        )
        return f"error: tool {name} failed: {exc}"
