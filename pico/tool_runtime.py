"""Tool execution, audit, and run-report helpers."""

import re

from . import tools as toolkit
from . import approval
from . import workspace_diff
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


def tool_capability(tool):
    if not tool:
        return ""
    return str(tool.get("capability", "write" if tool.get("risky") else "read"))


def tool_risk_level(tool):
    capability = tool_capability(tool)
    if capability in {"write", "execute"}:
        return "high"
    if capability == "delegate":
        return "medium"
    return "low"


def tool_permission_error(agent, tool):
    capability = tool_capability(tool)
    if agent.read_only and capability != "read":
        return {
            "code": "capability_denied",
            "security_event_type": "read_only_block",
            "message": f"error: permission denied for {capability} capability in read-only mode",
        }
    return None


def dry_run_tool_result(name, args):
    args = args or {}
    if name == "run_shell":
        return f"dry_run: would run shell command: {args.get('command', '')}"
    if name == "write_file":
        content = str(args.get("content", ""))
        return f"dry_run: would write {args.get('path', '')} ({len(content)} chars)"
    if name == "patch_file":
        return f"dry_run: would patch {args.get('path', '')}"
    return f"dry_run: would execute {name}"


def shell_policy_metadata(policy):
    if not policy:
        return {
            "shell_allowlisted": None,
            "shell_policy_reason": "",
            "shell_allowlist_match": "",
        }
    return {
        "shell_allowlisted": bool(policy.get("allowed")),
        "shell_policy_reason": str(policy.get("reason", "")),
        "shell_allowlist_match": str(policy.get("matched_prefix", "")),
    }


def shell_command_policy(name, args):
    if name != "run_shell":
        return None
    return toolkit.shell_command_policy((args or {}).get("command", ""))


def repeated_tool_call(agent, name, args):
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    if len(tool_events) < 2:
        return False
    recent = tool_events[-2:]
    return all(item["name"] == name and item["args"] == args for item in recent)


def run_tool(agent, name, args):
    tool = agent.tools.get(name)
    capability = tool_capability(tool)
    if tool is None:
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": "unknown_tool",
            "security_event_type": "",
            "risk_level": "high",
            "capability": "",
            "read_only": False,
            "dry_run": bool(agent.dry_run),
            "approval_required": False,
            "approval_decision": "not_required",
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
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
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": "invalid_arguments",
            "security_event_type": security_event_type,
            "risk_level": "high" if tool["risky"] else "low",
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": bool(agent.dry_run),
            "approval_required": False,
            "approval_decision": "not_required",
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        record_process_note_for_tool(agent, name, agent._last_tool_result_metadata)
        return message
    permission_error = tool_permission_error(agent, tool)
    if permission_error:
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": permission_error["code"],
            "security_event_type": permission_error["security_event_type"],
            "risk_level": tool_risk_level(tool),
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": bool(agent.dry_run),
            "approval_required": False,
            "approval_decision": "denied",
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        return permission_error["message"]
    if repeated_tool_call(agent, name, args):
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": "repeated_identical_call",
            "security_event_type": "",
            "risk_level": tool_risk_level(tool),
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": bool(agent.dry_run),
            "approval_required": False,
            "approval_decision": "not_required",
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
    shell_policy = shell_command_policy(name, args)
    if shell_policy and not shell_policy["allowed"] and agent.approval_policy == "never":
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": "shell_not_allowlisted",
            "security_event_type": "shell_not_allowlisted",
            "risk_level": tool_risk_level(tool),
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": bool(agent.dry_run),
            "approval_required": True,
            "approval_decision": "denied",
            "shell_allowlisted": False,
            "shell_policy_reason": shell_policy["reason"],
            "shell_allowlist_match": "",
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        return "error: shell command is not on the allowlist"
    if agent.dry_run and tool["risky"]:
        result = dry_run_tool_result(name, args)
        agent._last_tool_result_metadata = {
            "tool_status": "dry_run",
            "tool_error_code": "",
            "security_event_type": "",
            "risk_level": tool_risk_level(tool),
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": True,
            "approval_required": False,
            "approval_decision": "dry_run",
            **shell_policy_metadata(shell_policy),
            "affected_paths": [],
            "workspace_changed": False,
            "workspace_fingerprint": agent.workspace.fingerprint(),
            "diff_summary": [],
        }
        return result
    if tool["risky"] and not approval.approve(agent, name, args):
        agent._last_tool_result_metadata = {
            "tool_status": "rejected",
            "tool_error_code": "approval_denied",
            "security_event_type": "read_only_block" if agent.read_only else "approval_denied",
            "risk_level": "high",
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": bool(agent.dry_run),
            "approval_required": True,
            "approval_decision": "denied",
            **shell_policy_metadata(shell_policy),
            "affected_paths": [],
            "workspace_changed": False,
            "diff_summary": [],
        }
        return f"error: approval denied for {name}"
    before_snapshot, snapshot_mode = workspace_diff.before_workspace_snapshot(agent, name, args, tool)
    after_snapshot = before_snapshot
    try:
        result = clip(tool["run"](args))
        after_snapshot = workspace_diff.after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = workspace_diff.diff_workspace_snapshots(before_snapshot, after_snapshot)
        workspace_changed = bool(affected_paths)
        tool_status = "ok"
        tool_error_code = ""
        if name == "run_shell":
            match = re.search(r"exit_code:\s*(-?\d+)", result)
            exit_code = int(match.group(1)) if match else 0
            if exit_code != 0 and workspace_changed:
                tool_status = "partial_success"
                tool_error_code = "tool_partial_success"
            elif exit_code != 0:
                tool_status = "error"
                tool_error_code = "tool_failed"
        agent.update_memory_after_tool(name, args, result)
        agent._last_tool_result_metadata = {
            "tool_status": tool_status,
            "tool_error_code": tool_error_code,
            "security_event_type": "",
            "risk_level": tool_risk_level(tool),
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": False,
            "approval_required": bool(tool["risky"]),
            "approval_decision": "granted" if tool["risky"] else "not_required",
            **shell_policy_metadata(shell_policy),
            "affected_paths": affected_paths,
            "workspace_changed": workspace_changed,
            "workspace_fingerprint": agent.workspace.fingerprint(),
            "diff_summary": diff_summary,
        }
        record_process_note_for_tool(agent, name, agent._last_tool_result_metadata)
        return result
    except Exception as exc:
        after_snapshot = workspace_diff.after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = workspace_diff.diff_workspace_snapshots(before_snapshot, after_snapshot)
        workspace_changed = bool(affected_paths)
        security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
        if "protected write path blocked" in str(exc):
            security_event_type = "protected_write_path"
        agent._last_tool_result_metadata = {
            "tool_status": "partial_success" if workspace_changed else "error",
            "tool_error_code": "tool_partial_success" if workspace_changed else "tool_failed",
            "security_event_type": security_event_type,
            "risk_level": tool_risk_level(tool),
            "capability": capability,
            "read_only": capability == "read",
            "dry_run": False,
            "approval_required": bool(tool["risky"]),
            "approval_decision": "granted" if tool["risky"] else "not_required",
            **shell_policy_metadata(shell_policy),
            "affected_paths": affected_paths,
            "workspace_changed": workspace_changed,
            "workspace_fingerprint": agent.workspace.fingerprint(),
            "diff_summary": diff_summary,
        }
        record_process_note_for_tool(agent, name, agent._last_tool_result_metadata)
        return f"error: tool {name} failed: {exc}"
