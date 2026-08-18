"""Checkpoint and resume-state helpers."""

import uuid

from .features import memory as memorylib
from .workspace import clip, now

CHECKPOINT_SCHEMA_VERSION = "checkpoint-v7"
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"

RUNTIME_IDENTITY_KEYS = (
    "cwd",
    "model",
    "model_client",
    "provider_conversation_mode",
    "approval_policy",
    "read_only",
    "max_steps",
    "max_new_tokens",
    "provider_context_limit_tokens",
    "feature_flags",
    "shell_env_allowlist",
    "workspace_fingerprint",
    "tool_signature",
    "run_timeout_seconds",
    "verification_command",
    "sandbox_identity",
    "hooks",
)


def current_runtime_identity(agent):
    return {
        "session_id": agent.session.get("id", ""),
        "cwd": str(agent.root),
        "model": str(getattr(agent.model_client, "model", "")),
        "model_client": agent.model_client.__class__.__name__,
        "provider_conversation_mode": str(agent.model_client.conversation_mode),
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": agent.max_steps,
        "max_new_tokens": int(agent.max_new_tokens),
        "provider_context_limit_tokens": int(agent.provider_context_limit_tokens),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": getattr(getattr(agent, "prefix_state", None), "workspace_fingerprint", agent.workspace.fingerprint()),
        "tool_signature": agent.tool_signature(),
        "run_timeout_seconds": int(agent.run_timeout_seconds),
        "verification_command": str(agent.verification_command),
        "sandbox_identity": agent.sandbox.identity(),
        "hooks": agent.hooks.identity(),
    }


def checkpoint_state(agent):
    agent._ensure_session_shape()
    return agent.session["checkpoints"]


def current_checkpoint(agent):
    state = checkpoint_state(agent)
    checkpoint_id = str(state.get("current_id", "")).strip()
    if not checkpoint_id:
        return None
    return state.get("items", {}).get(checkpoint_id)


def task_state_from_checkpoint(agent, checkpoint):
    projection = agent.run_store.replay(checkpoint["context_run_id"])
    snapshot = projection.task_state(
        {
            "run_id": checkpoint["context_run_id"],
            "task_id": checkpoint["task_id"],
            "user_request": checkpoint["current_goal"],
            "checkpoint_id": checkpoint["checkpoint_id"],
            "resume_status": agent.resume_state.get("status", CHECKPOINT_NONE_STATUS),
        }
    )
    snapshot["checkpoint_id"] = checkpoint["checkpoint_id"]
    snapshot["user_request"] = checkpoint["current_goal"]
    return snapshot


def evaluate_resume_state(agent):
    previous_resume_state = dict(agent.session.get("resume_state", {}) or {})
    invalidated = agent.invalidate_stale_memory()
    checkpoint = current_checkpoint(agent)
    status = CHECKPOINT_NONE_STATUS
    stale_paths = list(invalidated)
    mismatch_fields = []
    if checkpoint:
        expected_fields = {
            "checkpoint_id", "parent_checkpoint_id", "schema_version", "created_at",
            "current_goal", "task_id", "key_files", "freshness", "summary",
            "runtime_identity", "context_run_id",
            "pending_partial_paths",
            "event_cursor", "event_hash", "workspace_content_fingerprint",
        }
        if set(checkpoint) != expected_fields or checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
        else:
            for item in checkpoint.get("key_files", []):
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                expected = item.get("freshness")
                current = memorylib.file_freshness(path, agent.root)
                if expected != current and path not in stale_paths:
                    stale_paths.append(path)
            saved_identity = dict(checkpoint.get("runtime_identity", {}) or agent.session.get("runtime_identity", {}) or {})
            current_identity = current_runtime_identity(agent)
            for key in RUNTIME_IDENTITY_KEYS:
                if key not in saved_identity or saved_identity.get(key) != current_identity.get(key):
                    mismatch_fields.append(key)
            if checkpoint.get("workspace_content_fingerprint") != agent.content_workspace_fingerprint():
                mismatch_fields.append("workspace_content_fingerprint")
            context_run_id = str(checkpoint.get("context_run_id", ""))
            if not agent.run_store.verify_event_cursor(
                context_run_id,
                checkpoint.get("event_cursor", 0),
                checkpoint.get("event_hash", ""),
            ):
                mismatch_fields.append("event_cursor")
            mismatch_fields.sort()
            if stale_paths:
                status = CHECKPOINT_PARTIAL_STALE_STATUS
            elif mismatch_fields:
                status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            else:
                status = CHECKPOINT_FULL_VALID_STATUS

    resume_state = {
        "status": status,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch_fields": mismatch_fields,
        "stale_summary_invalidations": max(
            len(invalidated),
            int(previous_resume_state.get("stale_summary_invalidations", 0))
            if status == CHECKPOINT_PARTIAL_STALE_STATUS
            else 0,
        ),
    }
    agent.session["resume_state"] = resume_state
    agent.session["runtime_identity"] = current_runtime_identity(agent)
    return resume_state


def render_checkpoint_text(agent):
    checkpoint = current_checkpoint(agent)
    if not checkpoint:
        return "Task checkpoint:\n- Resume status: no-checkpoint\n- No executable checkpoint."
    task_state = task_state_from_checkpoint(agent, checkpoint)
    lines = [
        "Task checkpoint:",
        f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
        f"- Current goal: {checkpoint.get('current_goal', '-') or '-'}",
        f"- Current blocker: {task_state.get('stop_reason', '-') or '-'}",
        f"- Next step: {infer_next_step(task_state)}",
    ]
    key_files = [str(item.get("path", "")).strip() for item in checkpoint.get("key_files", []) if str(item.get("path", "")).strip()]
    lines.append(f"- Key files: {', '.join(key_files) or '-'}")
    if task_state.get("final_answer"):
        lines.append("- Completed: " + str(task_state["final_answer"]))
    if agent.resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(agent.resume_state["stale_paths"]))
    summary = str(checkpoint.get("summary", "")).strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    return "\n".join(lines)


def infer_next_step(task_state):
    status = task_state.get("status", "") if isinstance(task_state, dict) else task_state.status
    stop_reason = (
        task_state.get("stop_reason", "")
        if isinstance(task_state, dict) else task_state.stop_reason
    )
    last_tool = task_state.get("last_tool", "") if isinstance(task_state, dict) else task_state.last_tool
    if status == "completed":
        return "No next step recorded."
    if stop_reason == "step_limit_reached":
        return "Resume from the latest checkpoint and continue the task."
    if last_tool:
        return f"Decide the next action after {last_tool}."
    return "Continue the task from the latest checkpoint."


def create_checkpoint(agent, task_state, user_message, trigger):
    state = checkpoint_state(agent)
    current = current_checkpoint(agent)
    checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
    key_files = []
    freshness = {}
    for path in agent.memory.to_dict()["working"]["recent_files"]:
        file_freshness = memorylib.file_freshness(path, agent.root)
        freshness[path] = file_freshness
        key_files.append({"path": path, "freshness": file_freshness})
    event_cursor = agent.run_store.event_cursor(task_state.run_id)
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "created_at": now(),
        "current_goal": str(user_message),
        "task_id": task_state.task_id,
        "key_files": key_files,
        "freshness": freshness,
        "summary": f"{trigger}: {clip(str(user_message), 120)}",
        "runtime_identity": current_runtime_identity(agent),
        "context_run_id": task_state.run_id,
        "pending_partial_paths": (
            list(agent.last_tool_outcome.affected_paths)
            if agent.last_tool_outcome is not None and agent.last_tool_outcome.status == "partial_success"
            else []
        ),
        "event_cursor": event_cursor.sequence,
        "event_hash": event_cursor.event_hash,
        "workspace_content_fingerprint": agent.content_workspace_fingerprint(),
    }
    # Only the latest executable state is retained; Runtime events preserve the history.
    state["items"] = {checkpoint_id: checkpoint}
    state["current_id"] = checkpoint_id
    task_state.checkpoint_id = checkpoint_id
    agent.session["runtime_identity"] = checkpoint["runtime_identity"]
    agent.session_path = agent.session_store.save(agent.session)
    return checkpoint
