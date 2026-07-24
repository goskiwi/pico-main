"""Run report and tool audit helpers."""

from . import security
from .workspace import clip


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
        "task_graph_path": str(agent.run_store.task_graph_path(task_state.run_id)),
        "agent": agent.identity_metadata(),
        "summary": build_run_summary(agent, task_state),
        "skills": dict((agent.last_prompt_metadata or {}).get("skills", {})),
        "repo_map": dict((agent.last_prompt_metadata or {}).get("repo_map", {})),
        "undo": (
            agent.current_undo_journal.summary()
            if getattr(agent, "current_undo_journal", None) is not None
            else {
                "schema_version": "run-undo-v1",
                "status": "unavailable",
                "available": False,
                "changed_path_count": 0,
                "changed_paths": [],
                "restored_paths": [],
                "manifest_path": "",
            }
        ),
        "tool_audit": list(agent.tool_audit_log),
        "model_action_rejections": list(getattr(agent, "model_action_rejections", [])),
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
        "undo_status": metadata.get("undo_status", "not_applicable"),
        "undo_recorded_paths": list(
            metadata.get("undo_recorded_paths") or []
        ),
        "result_preview": clip(result, 200),
    }
    if name == "run_shell":
        entry["command"] = clip(str((args or {}).get("command", "")), 200)
        entry["raw_output_chars"] = int(metadata.get("raw_output_chars") or 0)
        entry["summary_output_chars"] = int(metadata.get("summary_output_chars") or 0)
        entry["sandbox"] = {
            "backend": metadata.get("sandbox_backend", ""),
            "image": metadata.get("sandbox_image", ""),
            "network": metadata.get("sandbox_network", ""),
            "rootfs_read_only": bool(metadata.get("sandbox_rootfs_read_only")),
            "cpus": metadata.get("sandbox_cpus"),
            "memory": metadata.get("sandbox_memory", ""),
            "pids_limit": metadata.get("sandbox_pids_limit"),
            "timed_out": bool(metadata.get("sandbox_timed_out")),
        }
    elif name in {"read_file", "write_file", "patch_file", "search", "list_files", "delegate", "delegate_many"}:
        entry["path"] = clip(str((args or {}).get("path", ".")), 200)
        if name in {"delegate", "delegate_many"}:
            entry["delegate_outcome"] = dict(metadata.get("delegate_outcome") or {})
    elif name == "query_repo_map":
        entry["query"] = clip(str((args or {}).get("query", "")), 200)
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
    action_rejections = list(getattr(agent, "model_action_rejections", []))
    return {
        "task": clip(task_state.user_request, 300),
        "status": task_state.status,
        "stop_reason": task_state.stop_reason,
        "attempts": task_state.attempts,
        "tool_steps": task_state.tool_steps,
        "dry_run": bool(agent.dry_run),
        "tools": [entry.get("name", "") for entry in agent.tool_audit_log],
        "skills": list((agent.last_prompt_metadata or {}).get("skills", {}).get("selected_names", [])),
        "repo_map_files": list(
            (agent.last_prompt_metadata or {}).get("repo_map", {}).get("selected_files", [])
        ),
        "changed_files": changed_paths,
        "failed_tools": failed_tools,
        "security_events": security_events,
        "model_action_rejection_count": len(action_rejections),
        "model_action_rejection_reasons": [clip(item.get("reason", ""), 160) for item in action_rejections],
        "final_answer_preview": clip(task_state.final_answer, 300),
    }
