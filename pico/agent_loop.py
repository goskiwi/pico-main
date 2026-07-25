"""Execution lifecycle for one complete agent turn."""

import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from langsmith import tracing_context

from . import memory_runtime, report, run_undo, security
from . import tools as toolkit
from .actions import ACTION_FINAL, ACTION_RETRY, ACTION_TOOL, ModelAction
from .checkpoints import (
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from .config import (
    TASK_CANVAS_MAX_ACTIVE_NODES,
    TASK_CANVAS_MAX_TOKENS,
    TASK_CANVAS_RETAIN_NODES,
)
from .task_state import TaskState
from .workspace import clip, now


_COMPLETION_NOTICE = (
    "Runtime notice: a workspace change was followed by a successful pytest verification. "
    "The acceptance evidence is complete; submit_final now with the files changed and test result."
)


def _is_context_limit_error(exc):
    """Return whether a provider explicitly rejected the active conversation size."""
    message = str(exc).lower()
    indicators = (
        "context length",
        "context window",
        "maximum context",
        "max context",
        "prompt is too long",
        "input is too long",
        "too many tokens",
    )
    return any(indicator in message for indicator in indicators)


def _is_successful_pytest(name, args, metadata):
    verification = dict(metadata.get("verification") or {})
    return (
        name == "run_shell"
        and verification.get("framework") == "pytest"
        and verification.get("passed") is True
    )


def _record_completion_progress(progress_tracker, task_state, name, args, metadata):
    """Track deterministic completion evidence without trusting model narration."""
    if metadata.get("workspace_changed"):
        progress_tracker["last_workspace_change_step"] = task_state.tool_steps
        progress_tracker["last_recoverable_step"] = 0
        progress_tracker["completion_ready"] = False
        progress_tracker["completion_reason"] = ""
        progress_tracker["last_verification_status"] = "not_run"
        return
    verification = dict(metadata.get("verification") or {})
    if verification.get("framework") == "pytest":
        progress_tracker["last_verification_status"] = (
            "passed" if verification.get("passed") else "failed"
        )
        progress_tracker["last_verification_step"] = task_state.tool_steps
    if (
        name in {"write_file", "patch_file", "run_shell"}
        and str(metadata.get("tool_status", "")) in {"error", "rejected", "partial_success"}
    ):
        # A failed patch or public test is actionable evidence, not idle
        # exploration.  The model needs one bounded repair cycle to respond.
        progress_tracker["last_recoverable_step"] = task_state.tool_steps
    if (
        progress_tracker["last_workspace_change_step"]
        and _is_successful_pytest(name, args, metadata)
    ):
        progress_tracker["completion_ready"] = True
        progress_tracker["completion_reason"] = "workspace_change_followed_by_pytest_pass"


def _budget_notice(task_state, *, completion_ready):
    if completion_ready:
        return _COMPLETION_NOTICE
    if task_state.tool_steps >= task_state.hard_tool_limit:
        return "Runtime notice: the hard tool limit is reached; submit_final is the only action allowed."
    if task_state.tool_steps >= task_state.nominal_tool_budget:
        remaining = max(0, task_state.hard_tool_limit - task_state.tool_steps)
        return (
            "Runtime notice: the soft planning budget has been crossed. "
            f"You have at most {remaining} safety-limit tool calls remaining; use them only to "
            "repair, verify, or finish the current task."
        )
    return ""


class AgentTurnState(TypedDict, total=False):
    """Transient routing state; durable audit/resume state stays in ``TaskState``."""

    prompt_snapshot: tuple[str, dict[str, Any]] | None
    finalization_attempted: bool
    context_recovery_attempted: bool
    action: ModelAction
    result: str


def run_agent_turn(agent, user_message):
    """Run one user request through the bounded model/tool loop."""
    return _run_agent_turn(agent, user_message)


def _create_checkpoint(agent, task_state, user_message, trigger):
    checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
    agent.run_store.write_task_state(task_state)
    agent.emit_trace(
        task_state,
        "checkpoint_created",
        {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": trigger},
    )
    return checkpoint


def _record_prompt_checkpoints(agent, task_state, user_message, prompt_metadata):
    resume_status = prompt_metadata.get("resume_status")
    if resume_status == CHECKPOINT_PARTIAL_STALE_STATUS:
        _create_checkpoint(agent, task_state, user_message, "freshness_mismatch")
    elif resume_status == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
        agent.emit_trace(
            task_state,
            "runtime_identity_mismatch",
            {"fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", []))},
        )
        _create_checkpoint(agent, task_state, user_message, "workspace_mismatch")
    if prompt_metadata.get("budget_reductions"):
        _create_checkpoint(agent, task_state, user_message, "context_reduction")


def _prompt_for_attempt(agent, task_state, user_message, prompt_snapshot):
    """Build the first prompt once and reuse it for the Responses conversation."""
    started_at = time.monotonic()
    reused = prompt_snapshot is not None
    if reused:
        prompt, original_metadata = prompt_snapshot
        prompt_metadata = dict(original_metadata)
    else:
        prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
    prompt_metadata["prompt_reused"] = reused
    agent.emit_trace(
        task_state,
        "prompt_built",
        {
            "prompt_metadata": prompt_metadata,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        },
    )
    if not reused:
        _record_prompt_checkpoints(agent, task_state, user_message, prompt_metadata)
    return prompt, prompt_metadata


def _action_payload(action):
    if action.kind == ACTION_TOOL:
        return {"name": action.name, "args": action.args}
    if action.kind == ACTION_FINAL:
        return action.answer
    return action.error


def _record_action_result(agent, action, result):
    agent.model_client.record_action_result(action, result)


def _retry_notice(problem):
    return f"Runtime notice: {problem}. Return exactly one valid function call."


def _execute_tool_action(agent, task_state, user_message, action, progress_tracker):
    name = action.name
    args = action.args
    agent.mark_tool_planned(name)
    task_state.record_tool(name)
    started_at = time.monotonic()
    result = agent.run_tool(name, args)
    result = security.redact_text(agent, result)
    safe_args = security.redact_artifact(agent, args)
    duration_ms = int((time.monotonic() - started_at) * 1000)
    metadata = dict(agent._last_tool_result_metadata or {})

    report.record_tool_audit(agent, name, safe_args, result, duration_ms)
    _record_completion_progress(progress_tracker, task_state, name, args, metadata)
    tool_status = str(metadata.get("tool_status", "")).strip() or "ok"
    safe_name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(name or "tool"))
    node_id = f"N{task_state.tool_steps:03d}_{safe_name}"
    full_result = agent._last_tool_full_result
    if full_result is None:
        full_result = result
    full_result = security.redact_text(agent, full_result)
    result_ref = agent.run_store.save_reference(
        task_state,
        task_state.tool_steps,
        name,
        full_result,
    )
    agent.cache_read_only_evidence(
        name,
        args,
        full_result,
        result_ref=result_ref,
        node_id=node_id,
    )
    agent.mark_tool_finished(name, args, metadata, result)
    summary = agent.summarize_tool_result(name, safe_args, result)
    canvas_status = "done" if tool_status in {"ok", "dry_run"} else "blocked"
    agent.run_store.append_offload_event(
        task_state,
        node_id=node_id,
        tool_name=name,
        args=safe_args,
        summary=summary,
        status=canvas_status,
        result_ref=result_ref,
    )
    agent.run_store.append_task_node(
        task_state,
        node_id=node_id,
        summary=summary,
        status=canvas_status,
        result_ref=result_ref,
    )
    fold = agent.run_store.fold_task_canvas(
        task_state,
        token_counter=agent.count_tokens,
        max_active_nodes=TASK_CANVAS_MAX_ACTIVE_NODES,
        retain_nodes=TASK_CANVAS_RETAIN_NODES,
        max_tokens=TASK_CANVAS_MAX_TOKENS,
    )
    agent.record(
        {
            "role": "tool",
            "name": name,
            "node_id": node_id,
            "args": safe_args,
            "summary": summary,
            "result_ref": result_ref,
            "created_at": now(),
        }
    )
    agent.run_store.write_task_state(task_state)
    agent.run_store.update_index(task_state, latest_node_id=node_id)
    if fold["folded"]:
        agent.emit_trace(task_state, "task_canvas_folded", fold)
    agent.emit_trace(
        task_state,
        "tool_executed",
        {
            "name": name,
            "args": safe_args,
            "node_id": node_id,
            "result_ref": result_ref,
            "result": clip(result, 500),
            "duration_ms": duration_ms,
            **metadata,
        },
    )
    # Keep the provider-side tool conversation intact.  The next model turn
    # receives this exact result as the matching tool_result instead of a
    # lossy task-canvas summary.
    _record_action_result(agent, action, result)
    _create_checkpoint(agent, task_state, user_message, "tool_executed")


def _write_finished_run(agent, task_state, final, run_started_at):
    agent.emit_trace(
        task_state,
        "run_finished",
        {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": final,
            "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
        },
    )
    artifact = security.redact_artifact(agent, report.build_report(agent, task_state))
    agent.run_store.write_report(task_state, artifact)
    return final


def _run_agent_turn(agent, user_message):
    run_started_at = time.monotonic()
    agent.mark_work_started(user_message)
    agent.record({"role": "user", "content": user_message, "created_at": now()})

    task_state = TaskState.create(
        run_id=agent.new_run_id(),
        task_id=agent.new_task_id(),
        user_request=user_message,
        agent_mode=agent.agent_mode,
        parent_agent_id=agent.parent_agent_id,
    )
    hard_tool_limit = (
        agent.hard_max_steps if agent.feature_enabled("dynamic_budget") else agent.max_steps
    )
    task_state.configure_tool_budget(agent.max_steps, hard_tool_limit)
    task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
    agent.current_task_state = task_state
    agent.current_run_dir = agent.run_store.start_run(task_state)
    agent.current_undo_journal = run_undo.RunUndoJournal(
        agent.root,
        agent.current_run_dir,
        task_state.run_id,
    )
    agent.current_undo_journal.start()
    agent.tool_audit_log = []
    agent.model_action_rejections = []
    agent.model_client.reset_action_session()
    agent.emit_trace(
        task_state,
        "run_started",
        {
            "task_id": task_state.task_id,
            "user_request": clip(user_message, 300),
            **agent.identity_metadata(),
        },
    )

    def attempt_limit():
        active_tool_limit = (
            hard_tool_limit if task_state.step_extension_granted else task_state.nominal_tool_budget
        )
        return max(active_tool_limit * 3, active_tool_limit + 4)

    max_hard_attempts = max(hard_tool_limit * 3, hard_tool_limit + 4)
    progress_tracker = {
        "last_workspace_change_step": 0,
        "last_recoverable_step": 0,
        "completion_ready": False,
        "completion_reason": "",
        "last_verification_status": "not_run",
        "last_verification_step": 0,
    }
    action_tools = toolkit.responses_action_tools(agent.tools)
    final_action_tools = [tool for tool in action_tools if tool.get("name") == "submit_final"]

    def finish_stopped():
        if task_state.attempts >= attempt_limit() and task_state.tool_steps < hard_tool_limit:
            final = "Stopped after too many rejected model actions without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.mark_work_finished(final, stopped=True)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        _create_checkpoint(agent, task_state, user_message, task_state.stop_reason or "run_stopped")
        return _write_finished_run(agent, task_state, final, run_started_at)

    def call_model(state: AgentTurnState):
        if task_state.attempts >= attempt_limit():
            return Command(update={"result": finish_stopped()}, goto=END)

        completion_ready = bool(progress_tracker["completion_ready"])
        finalization_only = (
            completion_ready
            or task_state.tool_steps >= task_state.hard_tool_limit
        )
        finalization_attempted = bool(state.get("finalization_attempted", False))
        if finalization_only:
            if finalization_attempted:
                return Command(update={"result": finish_stopped()}, goto=END)
            finalization_attempted = True

        task_state.record_attempt()
        agent.run_store.write_task_state(task_state)
        prompt_snapshot = state.get("prompt_snapshot")
        prompt, prompt_metadata = _prompt_for_attempt(
            agent,
            task_state,
            user_message,
            prompt_snapshot,
        )
        if prompt_snapshot is None:
            budget_notice = _budget_notice(
                task_state,
                completion_ready=completion_ready,
            )
            if budget_notice:
                prompt = f"{prompt}\n\n{budget_notice}"
            prompt_metadata["tool_budget"] = {
                "nominal": task_state.nominal_tool_budget,
                "hard_limit": task_state.hard_tool_limit,
                "extension_granted": task_state.step_extension_granted,
                "completion_ready": completion_ready,
            }
            prompt_snapshot = (prompt, dict(prompt_metadata))

        agent.emit_trace(
            task_state,
            "model_requested",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "finalization_only": finalization_only,
                "completion_ready": completion_ready,
                "step_extension_granted": task_state.step_extension_granted,
                "hard_tool_limit": task_state.hard_tool_limit,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
            },
        )
        prompt_cache_key = None
        prompt_cache_retention = None
        if agent.feature_enabled("prompt_cache") and agent.model_client.supports_prompt_cache:
            prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            prompt_cache_retention = "in_memory"

        model_started_at = time.monotonic()
        try:
            action = agent.model_client.complete_action(
                prompt,
                agent.max_new_tokens,
                action_tools=final_action_tools if finalization_only else action_tools,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except Exception as exc:
            if _is_context_limit_error(exc) and not state.get("context_recovery_attempted", False):
                recovery = agent.prepare_context_recovery()
                agent.model_client.reset_action_session()
                agent.emit_trace(
                    task_state,
                    "context_recovery_started",
                    {
                        "reason": "provider_context_limit",
                        "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                        **recovery,
                    },
                )
                return Command(
                    update={
                        "prompt_snapshot": None,
                        "finalization_attempted": finalization_attempted,
                        "context_recovery_attempted": True,
                    },
                    goto="call_model",
                )
            error_type = type(exc).__name__
            error = str(exc)
            final = f"Stopped after model error: {error_type}: {error}"
            agent.mark_work_finished(final, stopped=True)
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.stop_model_error(final)
            agent.last_prompt_metadata = prompt_metadata
            agent.last_completion_metadata = {}
            _create_checkpoint(agent, task_state, user_message, "model_error")
            agent.emit_trace(
                task_state,
                "model_failed",
                {
                    "error_type": error_type,
                    "error": error,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )
            result = _write_finished_run(agent, task_state, final, run_started_at)
            return Command(update={"result": result}, goto=END)

        if state.get("context_recovery_attempted", False):
            agent.clear_context_recovery()
            agent.emit_trace(task_state, "context_recovery_completed", {})

        completion_metadata = dict(agent.model_client.last_completion_metadata or {})
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        kind = action.kind
        payload = _action_payload(action)

        if finalization_only and kind == ACTION_TOOL:
            rejected_tool_name = action.name
            finalization_reason = (
                "completion evidence is ready"
                if completion_ready
                else (
                    "the hard tool limit is reached"
                )
            )
            payload = _retry_notice(
                f"{finalization_reason}; only submit_final is allowed"
            )
            action = ModelAction.retry(
                payload,
                protocol="runtime_guard",
                raw_preview=action.raw_preview,
                call_id=action.call_id,
            )
            kind = ACTION_RETRY
            agent.emit_trace(
                task_state,
                "finalization_rejected",
                {
                    "reason": (
                        "completion_ready" if completion_ready else "hard_tool_limit_reached"
                    ),
                    "tool_name": rejected_tool_name,
                },
            )

        if (
            kind == ACTION_FINAL
            and agent.feature_enabled("require_workspace_change")
            and not any(entry.get("workspace_changed") for entry in agent.tool_audit_log)
        ):
            payload = _retry_notice(
                "the task requires a workspace change, but no effective file change was recorded"
            )
            action = ModelAction.retry(
                payload,
                protocol="runtime_guard",
                raw_preview=action.raw_preview,
            )
            kind = ACTION_RETRY
            agent.emit_trace(task_state, "final_rejected", {"reason": "workspace_change_required"})

        if (
            kind == ACTION_FINAL
            and progress_tracker["last_workspace_change_step"]
            and progress_tracker["last_verification_status"] == "failed"
        ):
            payload = _retry_notice(
                "the latest pytest verification failed after the workspace change; repair the failure "
                "or report that the task remains unverified"
            )
            action = ModelAction.retry(
                payload,
                protocol="runtime_guard",
                raw_preview=action.raw_preview,
                call_id=action.call_id,
            )
            kind = ACTION_RETRY
            agent.emit_trace(task_state, "final_rejected", {"reason": "verification_failed"})

        if kind == ACTION_RETRY:
            rejection = {
                "reason": payload,
                "protocol": action.protocol,
                "raw_preview": action.raw_preview,
            }
            agent.model_action_rejections.append(rejection)
            agent.emit_trace(task_state, "model_action_rejected", rejection)
        agent.emit_trace(
            task_state,
            "model_parsed",
            {
                "kind": kind,
                "action_protocol": action.protocol,
                "action_name": action.name,
                "completion_metadata": completion_metadata,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
            },
        )

        next_state = {
            "prompt_snapshot": prompt_snapshot,
            "finalization_attempted": finalization_attempted,
        }
        if kind == ACTION_TOOL:
            return Command(update={**next_state, "action": action}, goto="execute_tool")
        if kind == ACTION_RETRY:
            _record_action_result(agent, action, payload)
            agent.mark_retry_needed(payload)
            agent.record({"role": "assistant", "content": payload, "created_at": now()})
            agent.run_store.write_task_state(task_state)
            return Command(update=next_state, goto="call_model")

        final = str(payload).strip()
        agent.mark_work_finished(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        task_state.finish_success(final)
        memory_runtime.promote_durable_memory(agent, user_message, final)
        memory_runtime.llm_promote_durable_memory(agent, user_message, final)
        _create_checkpoint(agent, task_state, user_message, "run_finished")
        result = _write_finished_run(agent, task_state, final, run_started_at)
        return Command(update={"result": result}, goto=END)

    def execute_tool(state: AgentTurnState):
        _execute_tool_action(
            agent,
            task_state,
            user_message,
            state["action"],
            progress_tracker,
        )
        # The task canvas is a durable control-plane projection.  It must not
        # replace the live provider conversation: patching depends on the
        # exact source and test output returned by the preceding tool call.
        return Command(
            update={"prompt_snapshot": state.get("prompt_snapshot")},
            goto="call_model",
        )

    workflow = StateGraph(AgentTurnState)
    workflow.add_node(
        "call_model", call_model, destinations=("call_model", "execute_tool", END)
    )
    workflow.add_node("execute_tool", execute_tool, destinations=("call_model",))
    workflow.add_edge(START, "call_model")

    graph = workflow.compile()
    with tracing_context(enabled=False):
        result = graph.invoke(
            {"finalization_attempted": False},
            config={"recursion_limit": max(32, max_hard_attempts * 4 + 16)},
        )
    return result["result"]
