"""Tool execution, audit, and run-report helpers."""

import re

from . import approval
from . import security
from . import tool_policy
from . import workspace_diff
from .config import MAX_TOOL_OUTPUT
from .sandbox import SandboxError
from .workspace import clip


def _is_pytest_shell_command(shell_policy):
    matched_prefix = str((shell_policy or {}).get("matched_prefix", ""))
    return "pytest" in matched_prefix.split()


def _compact_pytest_result(result, limit=MAX_TOOL_OUTPUT):
    result = str(result)
    if len(result) <= limit:
        return result

    header_lines = re.findall(r"^(?:sandbox|exit_code):[^\n]*$", result, flags=re.MULTILINE)
    header = "\n".join(header_lines[:2])
    if header:
        header += "\n"
    summary_pattern = re.compile(
        r"(?i)(^(?:FAILED|ERROR)\s+|\b\d+\s+(?:failed|passed|errors?|skipped)\b)"
    )
    summary_lines = []
    for line in result.splitlines():
        stripped = line.strip()
        if stripped and summary_pattern.search(stripped) and stripped not in summary_lines:
            summary_lines.append(stripped[:500])
    summary = "\n".join(summary_lines[-3:])
    if summary:
        summary += "\n"
    marker = "...[pytest output compacted; full output is available in the tool artifact]...\n"
    summary_budget = max(0, min(1500, limit - len(header) - len(marker)))
    summary = summary[:summary_budget]
    tail_size = max(0, limit - len(header) - len(summary) - len(marker))
    tail = result[-tail_size:] if tail_size else ""
    return f"{header}{summary}{marker}{tail}"


def _pytest_verification(command, result, exit_code):
    """Return structured pytest truth that shell pipelines cannot mask."""
    if "pytest" not in str(command or "").lower():
        return None
    text = str(result or "")
    failed = sum(
        int(count)
        for count in re.findall(r"\b(\d+)\s+failed\b", text, flags=re.IGNORECASE)
    )
    errors = sum(
        int(count)
        for count in re.findall(r"\b(\d+)\s+errors?\b", text, flags=re.IGNORECASE)
    )
    failure_marker = bool(re.search(r"(?m)^(?:FAILED|ERROR)\s+", text))
    passed = int(exit_code) == 0 and not failed and not errors and not failure_marker
    return {
        "framework": "pytest",
        "passed": passed,
        "exit_code": int(exit_code),
        "failed": failed,
        "errors": errors,
        "pipeline_masked_failure": bool(int(exit_code) == 0 and not passed),
    }


def _merge_unique(left, right):
    return list(dict.fromkeys([*(left or []), *(right or [])]))


def _record_undo_changes(agent, token, affected_paths, diff_summary):
    if not token:
        return list(affected_paths or []), list(diff_summary or []), []
    journal = agent.current_undo_journal
    if journal is None:
        return list(affected_paths or []), list(diff_summary or []), []
    try:
        undo_paths, undo_summaries = journal.record(token)
    except Exception as exc:
        journal.mark_failed(token, exc)
        raise
    return (
        _merge_unique(affected_paths, undo_paths),
        _merge_unique(diff_summary, undo_summaries),
        list(undo_paths),
    )


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
    agent.memory.append_note(text, tags=tuple(tags), source=name)
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
    delegate_outcome=None,
    undo_status="not_applicable",
    undo_recorded_paths=None,
    raw_output_chars=None,
    summary_output_chars=None,
    verification=None,
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
        "undo_status": str(undo_status),
        "undo_recorded_paths": list(undo_recorded_paths or []),
    }
    if workspace_fingerprint is not None:
        metadata["workspace_fingerprint"] = workspace_fingerprint
    if sandbox_metadata:
        metadata.update(dict(sandbox_metadata))
    if delegate_outcome is not None:
        metadata["delegate_outcome"] = dict(delegate_outcome)
    if raw_output_chars is not None:
        metadata["raw_output_chars"] = int(raw_output_chars)
    if summary_output_chars is not None:
        metadata["summary_output_chars"] = int(summary_output_chars)
    if verification is not None:
        metadata["verification"] = dict(verification)
    return metadata


def _store_outcome(agent, name, tool, *, record_note=False, **updates):
    if name in {"delegate", "delegate_many"} and "delegate_outcome" not in updates:
        updates["delegate_outcome"] = dict(
            agent._delegate_outcome_metadata or {}
        )
    metadata = _result_metadata(agent, tool, **updates)
    agent._last_tool_result_metadata = metadata
    if record_note:
        record_process_note_for_tool(agent, name, metadata)
    return metadata


def _patch_conflict_evidence(agent, args):
    """Return the current file as repair evidence after an exact patch miss."""
    try:
        path = agent.path(args["path"])
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"error: patch conflict; current file could not be recovered: {exc}"
    return (
        "error: patch conflict; old_text no longer matches exactly. "
        "Use the current file content below as the repair source.\n\n"
        f"=== current file: {path.relative_to(agent.root)} ===\n{content}"
    )


def run_tool(agent, name, args):
    agent._last_tool_full_result = None
    agent._delegate_outcome_metadata = _unexecuted_delegate_outcome(name, args)
    tool = agent.tools.get(name)
    if tool is None:
        _store_outcome(
            agent, name, None, status="rejected", error_code="unknown_tool", risk_level="high"
        )
        return f"error: unknown tool '{name}'"
    if agent.is_duplicate_read_only_tool(name, args):
        cached = agent.cached_read_only_evidence(name, args)
        evidence = str(cached.get("result", "")).strip()
        node_id = str(cached.get("node_id", "")).strip()
        result_ref = str(cached.get("result_ref", "")).strip()
        _store_outcome(
            agent,
            name,
            tool,
            status="rejected",
            error_code="duplicate_read_only_call",
            workspace_fingerprint=agent.workspace.fingerprint(),
            record_note=True,
        )
        origin = ""
        if node_id or result_ref:
            origin = f" Original evidence: {node_id or 'saved result'} ({result_ref or 'in conversation'})."
        cached_result = clip(evidence, MAX_TOOL_OUTPUT) if evidence else "(cached output unavailable)"
        return (
            "error: duplicate read-only call blocked because the workspace has not changed. "
            "The original evidence is repeated below; use it instead of issuing the same call."
            f"{origin}\n\n{cached_result}"
        )
    try:
        agent.validate_tool(name, args)
    except Exception as exc:
        if name == "patch_file" and "old_text must occur exactly once" in str(exc):
            full_result = _patch_conflict_evidence(agent, args)
            agent._last_tool_full_result = full_result
            _store_outcome(
                agent,
                name,
                tool,
                status="rejected",
                error_code="patch_conflict",
                risk_level="high" if tool["risky"] else "low",
                record_note=True,
            )
            return clip(full_result)
        message = f"error: invalid arguments for {name}: {exc}"
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
    shell_policy = tool_policy.shell_command_policy(name, args)
    if shell_policy and not shell_policy["allowed"]:
        _store_outcome(
            agent,
            name,
            tool,
            status="rejected",
            error_code="shell_not_allowlisted",
            security_event_type="shell_not_allowlisted",
            approval_required=False,
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
    undo_token = None
    undo_journal = agent.current_undo_journal
    if undo_journal is not None:
        try:
            undo_token = undo_journal.prepare(agent, name, args, tool)
        except Exception as exc:
            _store_outcome(
                agent,
                name,
                tool,
                status="error",
                error_code="undo_snapshot_failed",
                security_event_type="undo_snapshot_failed",
                approval_required=bool(tool["risky"]),
                approval_decision="granted" if tool["risky"] else "not_required",
                shell_policy=shell_policy,
                undo_status="failed",
                record_note=True,
            )
            return f"error: undo snapshot failed before {name}: {exc}"
    before_snapshot, snapshot_mode = workspace_diff.before_workspace_snapshot(agent, name, args, tool)
    after_snapshot = before_snapshot
    if name == "run_shell":
        agent._last_sandbox_metadata = agent.sandbox.audit_metadata()
    try:
        full_result = security.redact_text(agent, tool["run"](args))
        agent._last_tool_full_result = full_result
        if name == "run_shell" and _is_pytest_shell_command(shell_policy):
            result = _compact_pytest_result(full_result)
        else:
            result = clip(full_result)
        after_snapshot = workspace_diff.after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = workspace_diff.diff_workspace_snapshots(before_snapshot, after_snapshot)
        try:
            affected_paths, diff_summary, undo_recorded_paths = _record_undo_changes(
                agent,
                undo_token,
                affected_paths,
                diff_summary,
            )
        except Exception as exc:
            _store_outcome(
                agent,
                name,
                tool,
                status="partial_success",
                error_code="undo_record_failed",
                security_event_type="undo_record_failed",
                dry_run=False,
                approval_required=bool(tool["risky"]),
                approval_decision="granted" if tool["risky"] else "not_required",
                shell_policy=shell_policy,
                affected_paths=affected_paths,
                workspace_changed=bool(affected_paths),
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
                sandbox_metadata=(
                    dict(agent._last_sandbox_metadata or {})
                    if name == "run_shell"
                    else {}
                ),
                undo_status="failed",
                record_note=True,
            )
            return f"error: {name} ran but its undo record failed: {exc}"
        workspace_changed = bool(affected_paths)
        tool_status = "ok"
        tool_error_code = ""
        security_event_type = ""
        verification = None
        sandbox_metadata = dict(agent._last_sandbox_metadata or {}) if name == "run_shell" else {}
        if name == "run_shell":
            match = re.search(r"exit_code:\s*(-?\d+)", result)
            exit_code = int(match.group(1)) if match else 0
            verification = _pytest_verification(args.get("command", ""), full_result, exit_code)
            if exit_code != 0 and workspace_changed:
                tool_status = "partial_success"
                tool_error_code = "tool_partial_success"
            elif exit_code != 0:
                tool_status = "error"
                tool_error_code = "tool_failed"
            if verification and not verification["passed"]:
                tool_status = "error"
                tool_error_code = "pytest_failed"
            if sandbox_metadata.get("sandbox_timed_out"):
                tool_error_code = "sandbox_timeout"
                security_event_type = "sandbox_timeout"
        elif name in {"delegate", "delegate_many"}:
            delegate_outcome = dict(agent._delegate_outcome_metadata or {})
            failed_count = int(delegate_outcome.get("failed_count") or 0)
            completed_count = int(delegate_outcome.get("completed_count") or 0)
            if failed_count:
                tool_status = "partial_success" if completed_count else "error"
                tool_error_code = (
                    "delegate_partial_success" if completed_count else "delegate_failed"
                )
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
            undo_status="recorded" if undo_token else "not_applicable",
            undo_recorded_paths=undo_recorded_paths,
            raw_output_chars=len(full_result) if name == "run_shell" else None,
            summary_output_chars=len(result) if name == "run_shell" else None,
            verification=verification,
            record_note=True,
        )
        return result
    except Exception as exc:
        after_snapshot = workspace_diff.after_workspace_snapshot(agent, name, args, tool, snapshot_mode, before_snapshot)
        affected_paths, diff_summary = workspace_diff.diff_workspace_snapshots(before_snapshot, after_snapshot)
        undo_record_error = None
        undo_recorded_paths = []
        try:
            affected_paths, diff_summary, undo_recorded_paths = _record_undo_changes(
                agent,
                undo_token,
                affected_paths,
                diff_summary,
            )
        except Exception as undo_exc:
            undo_record_error = undo_exc
        workspace_changed = bool(affected_paths)
        security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
        tool_error_code = (
            "undo_record_failed"
            if undo_record_error is not None
            else ("tool_partial_success" if workspace_changed else "tool_failed")
        )
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
            undo_status=(
                "failed"
                if undo_record_error is not None
                else ("recorded" if undo_token else "not_applicable")
            ),
            undo_recorded_paths=undo_recorded_paths,
            record_note=True,
        )
        if undo_record_error is not None:
            return (
                f"error: tool {name} failed: {exc}; "
                f"undo record also failed: {undo_record_error}"
            )
        return f"error: tool {name} failed: {exc}"


def _unexecuted_delegate_outcome(name, args):
    """Describe a rejected/failed delegate call without relying on result text."""
    if name not in {"delegate", "delegate_many"}:
        return {}
    if name == "delegate":
        specs = [args] if isinstance(args, dict) else []
    else:
        raw_tasks = args.get("tasks", []) if isinstance(args, dict) else []
        specs = list(raw_tasks) if isinstance(raw_tasks, list) else []
    items = []
    for index, spec in enumerate(specs, start=1):
        spec = spec if isinstance(spec, dict) else {}
        items.append(
            {
                "index": index,
                "role": str(spec.get("role", "")).strip(),
                "status": "not_run",
                "agent_id": "",
            }
        )
    return {
        "requested_count": len(specs),
        "completed_count": 0,
        "failed_count": len(specs),
        "items": items,
    }
