"""Execution lifecycle for one complete agent turn."""

import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from langsmith import tracing_context

from . import report
from .. import run_undo, security
from .. import tools as toolkit
from .actions import ACTION_FINAL, ACTION_RETRY, ACTION_TOOL, ModelAction
from .checkpoints import (
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from ..config import (
    MAX_CONSECUTIVE_INVALID_ACTIONS,
    TASK_CANVAS_MAX_ACTIVE_NODES,
    TASK_CANVAS_MAX_TOKENS,
    TASK_CANVAS_RETAIN_NODES,
)
from .verification import (
    run_runtime_verification,
    verification_feedback,
    verified_change_final,
)
from .state import read_only_tool_signature
from .task_state import TaskState
from ..workspace import clip


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


def _record_completion_progress(agent, progress_tracker, task_state, name, args, metadata):
    """Track workspace progress and invalidate superseded verifier evidence."""
    del args
    invalidated_count = agent.invalidate_runtime_verifications(
        name,
        metadata,
        tool_step=task_state.tool_steps,
    )
    if metadata.get("workspace_changed"):
        progress_tracker["last_workspace_change_step"] = task_state.tool_steps
    return invalidated_count


class AgentTurnState(TypedDict, total=False):
    """Transient routing state; durable audit/resume state stays in ``TaskState``."""

    prompt_snapshot: tuple[str, dict[str, Any]] | None
    context_compaction_trigger: str
    compact_after_tool: bool
    invalid_action_streak: int
    duplicate_read_only_signature: str
    duplicate_read_only_rejections: int
    action: ModelAction
    result: str


def run_agent_turn(agent, user_message):
    """Run one user request through the bounded model/tool loop."""
    return _run_agent_turn(agent, user_message)


def _create_checkpoint(agent, task_state, user_message, trigger):
    checkpoint = agent.create_checkpoint(task_state, user_message, trigger=trigger)
    agent.run_store.write_task_state(task_state)
    return checkpoint


def _record_prompt_checkpoints(agent, task_state, user_message, prompt_metadata):
    resume_status = prompt_metadata.get("resume_status")
    if resume_status == CHECKPOINT_PARTIAL_STALE_STATUS:
        _create_checkpoint(agent, task_state, user_message, "freshness_mismatch")
    elif resume_status == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
        _create_checkpoint(agent, task_state, user_message, "workspace_mismatch")
    if prompt_metadata.get("budget_reductions"):
        _create_checkpoint(agent, task_state, user_message, "context_reduction")


def _prompt_for_attempt(agent, task_state, user_message, prompt_snapshot):
    """Build the first prompt once and reuse it for the provider conversation."""
    reused = prompt_snapshot is not None
    if reused:
        prompt, original_metadata = prompt_snapshot
        prompt_metadata = dict(original_metadata)
    else:
        prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
    prompt_metadata["prompt_reused"] = reused
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


def _trace_tool_fields(name, args):
    """Project only the small, safe tool details useful in a live event."""
    fields = {"tool": str(name)}
    if name == "read_file":
        paths = [
            str(item.get("path", "")).strip()
            for item in args.get("files", [])
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        ]
        if paths:
            fields["paths"] = paths
            fields["target"] = ", ".join(paths[:3])
        return fields
    path = str(args.get("path", "")).strip()
    if path:
        fields["path"] = path
        fields["target"] = path
        return fields
    if name == "run_shell":
        command = clip(str(args.get("command", "")).strip(), 160)
        if command:
            fields["target"] = command
        return fields
    if name in {"delegate", "delegate_many"}:
        if name == "delegate":
            fields["target"] = str(args.get("role", "delegate")).strip() or "delegate"
        else:
            fields["target"] = f"tasks={len(args.get('tasks') or [])}"
        return fields
    query = str(args.get("query", "")).strip()
    if query:
        fields["target"] = clip(query, 160)
    return fields


def _empty_model_metrics():
    return {
        "calls": 0,
        "duration_ms": 0,
        "failures": 0,
        "action_rejections": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "compaction_calls": 0,
        "action_protocols": set(),
    }


def _nonnegative_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _record_model_completion(metrics, duration_ms, completion_metadata=None, protocol=""):
    metrics["duration_ms"] += _nonnegative_int(duration_ms)
    metadata = dict(completion_metadata or {})
    for field in ("input_tokens", "output_tokens", "cached_tokens"):
        metrics[field] += _nonnegative_int(metadata.get(field))
    if str(protocol).strip():
        metrics["action_protocols"].add(str(protocol).strip())


def _public_model_metrics(metrics):
    return {
        "calls": int(metrics["calls"]),
        "duration_ms": int(metrics["duration_ms"]),
        "failures": int(metrics["failures"]),
        "action_rejections": int(metrics["action_rejections"]),
        "input_tokens": int(metrics["input_tokens"]),
        "output_tokens": int(metrics["output_tokens"]),
        "cached_tokens": int(metrics["cached_tokens"]),
        "compaction_calls": int(metrics["compaction_calls"]),
        "action_protocols": sorted(metrics["action_protocols"]),
    }


def _execute_tool_action(agent, task_state, user_message, action, progress_tracker):
    name = action.name
    args = action.args
    agent.mark_tool_planned(name)
    task_state.record_tool(name)
    safe_args = security.redact_artifact(agent, args)
    trace_fields = _trace_tool_fields(name, safe_args)
    agent.emit_trace(task_state, "tool_start", trace_fields)
    started_at = time.monotonic()
    result = agent.run_tool(name, args)
    result = security.redact_text(agent, result)
    duration_ms = int((time.monotonic() - started_at) * 1000)
    metadata = dict(agent._last_tool_result_metadata or {})

    invalidated_verification_count = _record_completion_progress(
        agent,
        progress_tracker,
        task_state,
        name,
        args,
        metadata,
    )
    metadata["invalidated_runtime_verification_count"] = invalidated_verification_count
    agent._last_tool_result_metadata = metadata
    report.record_tool_audit(agent, name, safe_args, result, duration_ms)
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
    agent.run_store.fold_task_canvas(
        task_state,
        token_counter=agent.count_tokens,
        max_active_nodes=TASK_CANVAS_MAX_ACTIVE_NODES,
        retain_nodes=TASK_CANVAS_RETAIN_NODES,
        max_tokens=TASK_CANVAS_MAX_TOKENS,
    )
    agent.run_store.write_task_state(task_state)
    agent.run_store.update_index(task_state, latest_node_id=node_id)
    trace_payload = {
        **trace_fields,
        "status": tool_status,
        "error_code": str(metadata.get("tool_error_code", "")),
        "duration_ms": duration_ms,
        "workspace_changed": bool(metadata.get("workspace_changed")),
        "affected_paths": list(metadata.get("affected_paths") or []),
        "result_ref": result_ref,
        "result_preview": clip(result, 500),
        "result_chars": len(result),
        "invalidated_runtime_verification_count": invalidated_verification_count,
    }
    if "delegate_outcome" in metadata:
        trace_payload["delegate_outcome"] = dict(metadata["delegate_outcome"] or {})
    if "activated_skills" in metadata:
        trace_payload["activated_skills"] = list(metadata["activated_skills"] or [])
    agent.emit_trace(
        task_state,
        "tool_end",
        trace_payload,
    )
    # Keep the provider-side tool conversation intact.  The next model turn
    # receives this exact result as the matching tool_result instead of a
    # lossy task-canvas summary.
    _record_action_result(agent, action, result)
    _create_checkpoint(agent, task_state, user_message, "tool_executed")
    return metadata


def _write_finished_run(agent, task_state, final, run_started_at, model_metrics):
    public_model_metrics = _public_model_metrics(model_metrics)
    agent.last_run_model_metrics = public_model_metrics
    agent.emit_trace(
        task_state,
        "run_end",
        {
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer_preview": clip(final, 500),
            "tool_steps": task_state.tool_steps,
            "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            "model": public_model_metrics,
        },
    )
    artifact = security.redact_artifact(agent, report.build_report(agent, task_state))
    agent.run_store.write_report(task_state, artifact)
    return final


def _run_agent_turn(agent, user_message):
    run_started_at = time.monotonic()
    agent.mark_work_started(user_message)

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
    agent.start_trace(run_started_at)
    agent.current_undo_journal = run_undo.RunUndoJournal(
        agent.root,
        agent.current_run_dir,
        task_state.run_id,
    )
    agent.current_undo_journal.start()
    agent.tool_audit_log = []
    agent.runtime_verifications = []
    agent.model_action_rejections = []
    agent.model_client.reset_action_session()
    agent.start_task_skills()

    progress_tracker = {
        "last_workspace_change_step": 0,
        "repair_attempted": False,
        "last_runtime_verification": {},
    }
    model_metrics = _empty_model_metrics()
    # Scripted offline clients use the same flat Responses definitions as the
    # production client when they do not expose ``action_tools`` themselves.
    tool_builder = getattr(agent.model_client, "action_tools", toolkit.responses_action_tools)

    def finish_with_transition(final, *, stopped, trigger, transition):
        agent.mark_work_finished(final, stopped=stopped)
        transition(final)
        _create_checkpoint(agent, task_state, user_message, trigger)
        return _write_finished_run(
            agent, task_state, final, run_started_at, model_metrics
        )

    def finish_success(final, *, trigger):
        return finish_with_transition(
            final,
            stopped=False,
            trigger=trigger,
            transition=task_state.finish_success,
        )

    def finish_verification_failed(verification):
        command = str(verification.get("command", "verification command")).strip()
        final = (
            "Stopped after runtime verification failed following one repair attempt. "
            f"Command: {command}."
        )
        return finish_with_transition(
            final,
            stopped=True,
            trigger="runtime_verification_failed",
            transition=task_state.stop_verification_failed,
        )

    def finish_verification_stale(verification):
        command = str(verification.get("command", "verification command")).strip()
        final = (
            "Stopped because the workspace changed after runtime verification passed. "
            f"Run {command} again against the current workspace."
        )
        return finish_with_transition(
            final,
            stopped=True,
            trigger="runtime_verification_stale",
            transition=task_state.stop_verification_stale,
        )

    def finish_duplicate_read_only_loop():
        final = (
            "Stopped after the same read-only call was rejected twice as a duplicate. "
            "Use the cached evidence or choose a different tool call."
        )
        return finish_with_transition(
            final,
            stopped=True,
            trigger="duplicate_read_only_loop",
            transition=task_state.stop_duplicate_read_only_loop,
        )

    def finish_invalid_action_limit():
        final = (
            "Stopped after 8 consecutive invalid model actions. "
            "Submit a valid tool call or final answer."
        )
        return finish_with_transition(
            final,
            stopped=True,
            trigger="invalid_action_limit",
            transition=task_state.stop_invalid_action_limit,
        )

    def finish_requested_tool_limit():
        final = (
            f"Stopped after reaching the configured {agent.max_steps} tool call limit."
        )
        return finish_with_transition(
            final,
            stopped=True,
            trigger="requested_tool_limit",
            transition=task_state.stop_requested_tool_limit,
        )

    def call_model(state: AgentTurnState):
        if task_state.tool_steps >= agent.max_steps:
            return Command(update={"result": finish_requested_tool_limit()}, goto=END)

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
            prompt_snapshot = (prompt, dict(prompt_metadata))

        prompt_cache_key = None
        prompt_cache_retention = None
        if agent.feature_enabled("prompt_cache") and agent.model_client.supports_prompt_cache:
            prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            prompt_cache_retention = "in_memory"

        model_metrics["calls"] += 1
        agent.emit_trace(
            task_state,
            "model_start",
            {
                "attempt": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                **agent.identity_metadata(),
            },
        )
        model_started_at = time.monotonic()
        try:
            action_tools = tool_builder(agent.tools)
            action = agent.model_client.complete_action(
                prompt,
                agent.max_new_tokens,
                action_tools=action_tools,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except Exception as exc:
            if _is_context_limit_error(exc) and agent.can_compact_task_context(task_state):
                _record_model_completion(
                    model_metrics,
                    int((time.monotonic() - model_started_at) * 1000),
                )
                return Command(
                    update={
                        "context_compaction_trigger": "provider_overflow",
                    },
                    goto="compact_context",
                )
            error_type = type(exc).__name__
            error = str(exc)
            final = f"Stopped after model error: {error_type}: {error}"
            agent.mark_work_finished(final, stopped=True)
            task_state.stop_model_error(final)
            agent.last_prompt_metadata = prompt_metadata
            agent.last_completion_metadata = {}
            _create_checkpoint(agent, task_state, user_message, "model_error")
            _record_model_completion(
                model_metrics,
                int((time.monotonic() - model_started_at) * 1000),
            )
            model_metrics["failures"] += 1
            result = _write_finished_run(
                agent, task_state, final, run_started_at, model_metrics
            )
            return Command(update={"result": result}, goto=END)

        completion_metadata = dict(agent.model_client.last_completion_metadata or {})
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        kind = action.kind
        payload = _action_payload(action)
        _record_model_completion(
            model_metrics,
            int((time.monotonic() - model_started_at) * 1000),
            completion_metadata,
            action.protocol,
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

        if kind == ACTION_RETRY:
            rejection = {
                "reason": payload,
                "protocol": action.protocol,
                "raw_preview": action.raw_preview,
            }
            agent.model_action_rejections.append(rejection)
            model_metrics["action_rejections"] += 1

        if (
            kind == ACTION_FINAL
            and progress_tracker["last_workspace_change_step"]
            and agent.workspace.verification_command
        ):
            verification = run_runtime_verification(agent, task_state)
            progress_tracker["last_runtime_verification"] = verification
            if agent.runtime_verification_is_current(verification):
                final = verified_change_final(agent, progress_tracker)
                return Command(
                    update={"result": finish_success(final, trigger="runtime_verification_passed")},
                    goto=END,
                )
            if verification["status"] == "passed":
                return Command(
                    update={"result": finish_verification_stale(verification)},
                    goto=END,
                )
            if verification["status"] == "infrastructure_error":
                return Command(
                    update={"result": finish_verification_failed(verification)},
                    goto=END,
                )
            if progress_tracker["repair_attempted"]:
                return Command(
                    update={"result": finish_verification_failed(verification)},
                    goto=END,
                )
            progress_tracker["repair_attempted"] = True
            feedback = verification_feedback(verification)
            _record_action_result(agent, action, feedback)
            agent.mark_retry_needed(feedback)
            _create_checkpoint(agent, task_state, user_message, "runtime_verification_failed")
            return Command(
                update={
                    "prompt_snapshot": prompt_snapshot,
                    "invalid_action_streak": 0,
                },
                goto="call_model",
            )

        next_state = {
            "prompt_snapshot": prompt_snapshot,
            "invalid_action_streak": 0,
        }
        if kind == ACTION_TOOL:
            return Command(
                update={
                    **next_state,
                    "action": action,
                    "compact_after_tool": agent.input_reaches_context_threshold(
                        completion_metadata.get("input_tokens")
                    ),
                },
                goto="execute_tool",
            )
        if kind == ACTION_RETRY:
            _record_action_result(agent, action, payload)
            agent.mark_retry_needed(payload)
            agent.run_store.write_task_state(task_state)
            invalid_action_streak = int(state.get("invalid_action_streak", 0)) + 1
            if invalid_action_streak >= MAX_CONSECUTIVE_INVALID_ACTIONS:
                return Command(
                    update={"result": finish_invalid_action_limit()},
                    goto=END,
                )
            retry_state = {
                **next_state,
                "invalid_action_streak": invalid_action_streak,
                "duplicate_read_only_signature": "",
                "duplicate_read_only_rejections": 0,
            }
            if agent.needs_context_compaction(
                task_state,
                completion_metadata.get("input_tokens"),
            ):
                return Command(
                    update={
                        **retry_state,
                        "context_compaction_trigger": "input_threshold",
                    },
                    goto="compact_context",
                )
            return Command(update=retry_state, goto="call_model")

        final = str(payload).strip()
        result = finish_success(final, trigger="run_end")
        return Command(update={"result": result}, goto=END)

    def execute_tool(state: AgentTurnState):
        metadata = _execute_tool_action(
            agent,
            task_state,
            user_message,
            state["action"],
            progress_tracker,
        )
        duplicate_signature = ""
        duplicate_rejections = 0
        if metadata.get("tool_error_code") == "duplicate_read_only_call":
            duplicate_signature = read_only_tool_signature(
                state["action"].name,
                state["action"].args,
            )
            previous_signature = str(state.get("duplicate_read_only_signature", ""))
            previous_rejections = int(state.get("duplicate_read_only_rejections", 0))
            duplicate_rejections = (
                previous_rejections + 1
                if duplicate_signature == previous_signature
                else 1
            )
            if duplicate_rejections >= 2:
                return Command(
                    update={"result": finish_duplicate_read_only_loop()},
                    goto=END,
                )

        duplicate_state = {
            "duplicate_read_only_signature": duplicate_signature,
            "duplicate_read_only_rejections": duplicate_rejections,
        }
        if state.get("compact_after_tool", False) and agent.can_compact_task_context(task_state):
            return Command(
                update={
                    **duplicate_state,
                    "context_compaction_trigger": "input_threshold",
                },
                goto="compact_context",
            )
        return Command(
            update={
                **duplicate_state,
                "prompt_snapshot": state.get("prompt_snapshot"),
            },
            goto="call_model",
        )

    def compact_context(state: AgentTurnState):
        trigger = str(state.get("context_compaction_trigger", "provider_overflow"))
        if not agent.can_compact_task_context(task_state):
            final = "Stopped because provider context was exhausted before a task checkpoint could be created."
            agent.mark_work_finished(final, stopped=True)
            task_state.stop_model_error(final)
            _create_checkpoint(agent, task_state, user_message, "context_compaction_unavailable")
            return Command(
                update={"result": _write_finished_run(agent, task_state, final, run_started_at, model_metrics)},
                goto=END,
            )

        agent.emit_trace(
            task_state,
            "compaction_start",
            {
                "trigger": trigger,
                "sequence": len(task_state.context_compactions) + 1,
                "tool_steps": task_state.tool_steps,
            },
        )
        model_metrics["calls"] += 1
        model_metrics["compaction_calls"] += 1
        agent.emit_trace(
            task_state,
            "model_start",
            {
                "attempt": task_state.attempts,
                "tool_steps": task_state.tool_steps,
                "kind": "context_compaction",
                "trigger": trigger,
                **agent.identity_metadata(),
            },
        )
        started_at = time.monotonic()
        try:
            record = agent.compact_task_context(task_state, user_message, trigger)
        except Exception as exc:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            _record_model_completion(model_metrics, duration_ms)
            model_metrics["failures"] += 1
            final = f"Stopped after context compaction failed: {type(exc).__name__}: {exc}"
            agent.mark_work_finished(final, stopped=True)
            task_state.stop_model_error(final)
            _create_checkpoint(agent, task_state, user_message, "context_compaction_failed")
            agent.emit_trace(
                task_state,
                "compaction_end",
                {
                    "trigger": trigger,
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "error": str(exc),
                },
            )
            return Command(
                update={"result": _write_finished_run(agent, task_state, final, run_started_at, model_metrics)},
                goto=END,
            )

        completion_metadata = dict(agent.model_client.last_completion_metadata or {})
        _record_model_completion(
            model_metrics,
            int(record.get("duration_ms", 0)),
            completion_metadata,
            "context_compaction",
        )
        agent.model_client.reset_action_session()
        agent.emit_trace(
            task_state,
            "compaction_end",
            {
                "trigger": trigger,
                "status": "completed",
                "sequence": record["sequence"],
                "duration_ms": int(record["duration_ms"]),
                "checkpoint_tokens": int(record["checkpoint_tokens"]),
                "recent_evidence_tokens": int(record["recent_evidence_tokens"]),
                "workspace_fingerprint": record["workspace_fingerprint"],
                "artifact_path": record["artifact_path"],
            },
        )
        return Command(
            update={
                "prompt_snapshot": None,
            },
            goto="call_model",
        )

    workflow = StateGraph(AgentTurnState)
    workflow.add_node(
        "call_model", call_model, destinations=("call_model", "execute_tool", "compact_context", END)
    )
    workflow.add_node("execute_tool", execute_tool, destinations=("call_model", "compact_context"))
    workflow.add_node("compact_context", compact_context, destinations=("call_model", END))
    workflow.add_edge(START, "call_model")

    graph = workflow.compile()
    with tracing_context(enabled=False):
        result = graph.invoke(
            {
                "invalid_action_streak": 0,
                "duplicate_read_only_signature": "",
                "duplicate_read_only_rejections": 0,
            },
            # LangGraph requires a recursion fuse. This is not a task or tool
            # budget; ordinary stopping remains governed by the runtime rules.
            config={"recursion_limit": 10_000},
        )
    return result["result"]
