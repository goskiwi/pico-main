"""Tool execution, audit, and run-report helpers."""

import hashlib
import re
import subprocess

from . import tools as toolkit
from . import security
from .config import IGNORED_PATH_NAMES
from .workspace import clip


def capture_workspace_snapshot(agent):
    snapshot = {}
    for path in agent.root.rglob("*"):
        try:
            relative_parts = path.relative_to(agent.root).parts
        except ValueError:
            continue
        if any(part in IGNORED_PATH_NAMES for part in relative_parts):
            continue
        if not path.is_file():
            continue
        try:
            snapshot[path.relative_to(agent.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            continue
    return snapshot


def capture_path_snapshot(agent, paths):
    snapshot = {}
    for raw_path in paths:
        if not str(raw_path or "").strip():
            continue
        try:
            path = agent.path(raw_path)
            relative_path = path.relative_to(agent.root).as_posix()
            relative_parts = path.relative_to(agent.root).parts
        except Exception:
            continue
        if any(part in IGNORED_PATH_NAMES for part in relative_parts):
            continue
        if not path.exists():
            snapshot[relative_path] = None
            continue
        if not path.is_file():
            continue
        try:
            snapshot[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
        except Exception:
            continue
    return snapshot


def capture_git_status_snapshot(agent):
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=agent.root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except Exception:
        return None

    snapshot = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        status = line[:2]
        path_text = line[3:] if len(line) > 3 else ""
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        path_text = path_text.strip().strip('"')
        if not path_text:
            continue
        parts = tuple(part for part in path_text.split("/") if part)
        if any(part in IGNORED_PATH_NAMES for part in parts):
            continue
        snapshot[path_text] = status
    return snapshot


def diff_workspace_snapshots(before, after):
    changed_paths = []
    summaries = []
    all_paths = sorted(set(before) | set(after))
    for path in all_paths:
        if before.get(path) == after.get(path):
            continue
        changed_paths.append(path)
        if before.get(path) is None and after.get(path) is not None:
            summaries.append(f"created:{path}")
        elif before.get(path) is not None and after.get(path) is None:
            summaries.append(f"deleted:{path}")
        elif path not in before:
            after_status = str(after.get(path, ""))
            if "D" in after_status:
                summaries.append(f"deleted:{path}")
            elif after_status.strip() == "??" or "A" in after_status:
                summaries.append(f"created:{path}")
            else:
                summaries.append(f"modified:{path}")
        elif path not in after:
            before_status = str(before.get(path, ""))
            if "D" in before_status:
                summaries.append(f"modified:{path}")
            else:
                summaries.append(f"deleted:{path}")
        else:
            summaries.append(f"modified:{path}")
    return changed_paths, summaries


def target_snapshot_paths(name, args):
    if name in {"write_file", "patch_file"}:
        return [str((args or {}).get("path", ""))]
    return []


def before_workspace_snapshot(agent, name, args, tool):
    if not tool["risky"]:
        return {}, "none"
    paths = target_snapshot_paths(name, args)
    if paths:
        return capture_path_snapshot(agent, paths), "paths"
    if name == "run_shell":
        git_snapshot = capture_git_status_snapshot(agent)
        if git_snapshot is not None:
            return git_snapshot, "git_status"
    return capture_workspace_snapshot(agent), "full"


def after_workspace_snapshot(agent, name, args, tool, mode, before_snapshot):
    if not tool["risky"]:
        return before_snapshot
    if mode == "paths":
        return capture_path_snapshot(agent, target_snapshot_paths(name, args))
    if mode == "git_status":
        git_snapshot = capture_git_status_snapshot(agent)
        if git_snapshot is not None:
            return git_snapshot
    if mode == "none":
        return before_snapshot
    return capture_workspace_snapshot(agent)


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
    if tool["risky"] and not agent.approve(name, args):
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
    before_snapshot, snapshot_mode = before_workspace_snapshot(agent, name, args, tool)
    after_snapshot = before_snapshot
    try:
        result = clip(tool["run"](args))
        after_snapshot = after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = diff_workspace_snapshots(before_snapshot, after_snapshot)
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
        after_snapshot = after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = diff_workspace_snapshots(before_snapshot, after_snapshot)
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


def build_report(agent, task_state):
    return {
        "run_id": task_state.run_id,
        "task_id": task_state.task_id,
        "status": task_state.status,
        "stop_reason": task_state.stop_reason,
        "final_answer": task_state.final_answer,
        "tool_steps": task_state.tool_steps,
        "attempts": task_state.attempts,
        "checkpoint_id": task_state.checkpoint_id,
        "resume_status": task_state.resume_status,
        "dry_run": bool(agent.dry_run),
        "agent": agent.identity_metadata(),
        "summary": build_run_summary(agent, task_state),
        "tool_audit": list(agent.tool_audit_log),
        "task_state": task_state.to_dict(),
        "prompt_metadata": agent.last_prompt_metadata,
        "durable_promotions": list(agent.last_durable_promotions),
        "durable_rejections": list(agent.last_durable_rejections),
        "durable_superseded": list(agent.last_durable_superseded),
        "llm_durable_promotions": list(getattr(agent, "last_llm_durable_promotions", [])),
        "llm_durable_rejections": list(getattr(agent, "last_llm_durable_rejections", [])),
        "llm_durable_superseded": list(getattr(agent, "last_llm_durable_superseded", [])),
        "llm_memory_extractor_error": str(getattr(agent, "last_llm_memory_extractor_error", "")),
        "redacted_env": security.detected_secret_env_summary(agent),
    }


def record_tool_audit(agent, name, args, result, duration_ms):
    metadata = dict(agent._last_tool_result_metadata or {})
    entry = {
        "name": str(name or ""),
        "status": metadata.get("tool_status", ""),
        "error_code": metadata.get("tool_error_code", ""),
        "security_event_type": metadata.get("security_event_type", ""),
        "capability": metadata.get("capability", ""),
        "risk_level": metadata.get("risk_level", ""),
        "dry_run": bool(metadata.get("dry_run", False)),
        "approval_required": bool(metadata.get("approval_required", False)),
        "approval_decision": metadata.get("approval_decision", ""),
        "shell_allowlisted": metadata.get("shell_allowlisted"),
        "shell_policy_reason": metadata.get("shell_policy_reason", ""),
        "shell_allowlist_match": metadata.get("shell_allowlist_match", ""),
        "duration_ms": int(duration_ms),
        "affected_paths": list(metadata.get("affected_paths") or []),
        "workspace_changed": bool(metadata.get("workspace_changed")),
        "diff_summary": list(metadata.get("diff_summary") or []),
        "result_preview": clip(result, 200),
    }
    if name == "run_shell":
        entry["command"] = clip(str((args or {}).get("command", "")), 200)
    elif name in {"read_file", "write_file", "patch_file", "search", "list_files", "delegate", "delegate_many"}:
        entry["path"] = clip(str((args or {}).get("path", ".")), 200)
    agent.tool_audit_log.append(entry)
    return entry


def build_run_summary(agent, task_state):
    changed_paths = sorted({path for entry in agent.tool_audit_log for path in entry.get("affected_paths", [])})
    failed_tools = [
        {
            "name": entry.get("name", ""),
            "status": entry.get("status", ""),
            "error_code": entry.get("error_code", ""),
        }
        for entry in agent.tool_audit_log
        if entry.get("status") not in {"", "ok"}
    ]
    security_events = [
        {
            "name": entry.get("name", ""),
            "type": entry.get("security_event_type", ""),
            "error_code": entry.get("error_code", ""),
        }
        for entry in agent.tool_audit_log
        if entry.get("security_event_type")
    ]
    return {
        "task": clip(task_state.user_request, 300),
        "status": task_state.status,
        "stop_reason": task_state.stop_reason,
        "attempts": task_state.attempts,
        "tool_steps": task_state.tool_steps,
        "dry_run": bool(agent.dry_run),
        "tools": [entry.get("name", "") for entry in agent.tool_audit_log],
        "changed_files": changed_paths,
        "failed_tools": failed_tools,
        "security_events": security_events,
        "final_answer_preview": clip(task_state.final_answer, 300),
    }
