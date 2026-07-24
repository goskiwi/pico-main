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
from .parser import retry_notice
from .task_state import TaskState
from .workspace import clip, now


class AgentTurnState(TypedDict, total=False):
    """Transient routing state; durable audit/resume state stays in ``TaskState``."""

    native_prompt: tuple[str, dict[str, Any]] | None
    finalization_attempted: bool
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


def _prompt_for_attempt(agent, task_state, user_message, native_prompt):
    """Build text prompts each attempt; reuse the first prompt for native Actions."""
    started_at = time.monotonic()
    reused = native_prompt is not None
    if reused:
        prompt, original_metadata = native_prompt
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
    recorder = getattr(agent.model_client, "record_action_result", None)
    if recorder is not None:
        recorder(action, result)


def _execute_tool_action(agent, task_state, user_message, action):
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
    agent.mark_tool_finished(name, metadata, result)
    tool_status = str(metadata.get("tool_status", "")).strip() or "ok"
    safe_name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(name or "tool"))
    node_id = f"t{task_state.tool_steps:03d}_{safe_name}"
    content_ref = agent.run_store.save_tool_output(task_state, task_state.tool_steps, name, result)
    agent.run_store.append_task_graph_tool(task_state, node_id, name, safe_args, tool_status, content_ref)
    agent.record(
        {
            "role": "tool",
            "name": name,
            "node_id": node_id,
            "args": safe_args,
            "summary": agent.summarize_tool_result(name, safe_args, result),
            "content_ref": content_ref,
            "created_at": now(),
        }
    )
    agent.run_store.write_task_state(task_state)
    agent.emit_trace(
        task_state,
        "tool_executed",
        {
            "name": name,
            "args": safe_args,
            "result": clip(result, 500),
            "duration_ms": duration_ms,
            **metadata,
        },
    )
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
    reset_action_session = getattr(agent.model_client, "reset_action_session", None)
    if reset_action_session is not None:
        reset_action_session()
    agent.emit_trace(
        task_state,
        "run_started",
        {
            "task_id": task_state.task_id,
            "user_request": clip(user_message, 300),
            **agent.identity_metadata(),
        },
    )

    max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
    native_actions = bool(getattr(agent.model_client, "supports_native_actions", False))
    action_tools = toolkit.responses_action_tools(agent.tools)
    final_action_tools = [tool for tool in action_tools if tool.get("name") == "submit_final"]

    def finish_stopped():
        if task_state.attempts >= max_attempts and task_state.tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.mark_work_finished(final, stopped=True)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        _create_checkpoint(agent, task_state, user_message, task_state.stop_reason or "run_stopped")
        return _write_finished_run(agent, task_state, final, run_started_at)

    def call_model(state: AgentTurnState):
        if task_state.attempts >= max_attempts:
            return Command(update={"result": finish_stopped()}, goto=END)

        finalization_only = task_state.tool_steps >= agent.max_steps
        finalization_attempted = bool(state.get("finalization_attempted", False))
        if finalization_only:
            if not native_actions or finalization_attempted:
                return Command(update={"result": finish_stopped()}, goto=END)
            finalization_attempted = True

        task_state.record_attempt()
        agent.run_store.write_task_state(task_state)
        native_prompt = state.get("native_prompt")
        prompt, prompt_metadata = _prompt_for_attempt(
            agent,
            task_state,
            user_message,
            native_prompt if native_actions else None,
        )
        if native_actions and native_prompt is None:
            native_prompt = (prompt, dict(prompt_metadata))

        agent.emit_trace(
            task_state,
            "model_requested",
            {
                "attempts": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "finalization_only": finalization_only,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
            },
        )
        prompt_cache_key = None
        prompt_cache_retention = None
        if agent.feature_enabled("prompt_cache") and getattr(
            agent.model_client, "supports_prompt_cache", False
        ):
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
                require_explicit_final=agent.feature_enabled("require_explicit_final"),
            )
        except Exception as exc:
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

        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        kind = action.kind
        payload = _action_payload(action)

        if finalization_only and kind == ACTION_TOOL:
            rejected_tool_name = action.name
            payload = retry_notice(
                "the tool step budget is exhausted; only submit_final is allowed"
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
                {"reason": "tool_step_budget_exhausted", "tool_name": rejected_tool_name},
            )

        if (
            kind == ACTION_FINAL
            and agent.feature_enabled("require_workspace_change")
            and not any(entry.get("workspace_changed") for entry in agent.tool_audit_log)
        ):
            payload = retry_notice(
                "the task requires a workspace change, but no effective file change was recorded"
            )
            action = ModelAction.retry(
                payload,
                protocol="runtime_guard",
                raw_preview=action.raw_preview,
            )
            kind = ACTION_RETRY
            agent.emit_trace(task_state, "final_rejected", {"reason": "workspace_change_required"})

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
            "native_prompt": native_prompt,
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
        _execute_tool_action(agent, task_state, user_message, state["action"])
        return Command(goto="call_model")

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
            config={"recursion_limit": max(32, max_attempts * 4 + 16)},
        )
    return result["result"]
