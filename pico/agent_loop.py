"""Execution lifecycle for one complete agent turn."""

import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from langsmith import tracing_context

from . import report, run_undo, security
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
    DEFAULT_RUNTIME_VERIFICATION_TIMEOUT_SECONDS,
)
from .task_state import TaskState
from .workspace import clip


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


def _budget_notice(task_state):
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


def _verification_output(stdout, stderr, *, limit=3200):
    output = "\n".join(part for part in (str(stdout or "").strip(), str(stderr or "").strip()) if part)
    if len(output) <= limit:
        return output
    return f"...[runtime verification output truncated]...\n{output[-limit:]}"


def _run_runtime_verification(agent, task_state):
    """Run the user-configured verifier outside the model's tool/approval path."""
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
    record["workspace_fingerprint"] = agent.verification_workspace_fingerprint()
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


def _verification_feedback(record):
    command = str(record.get("command", ""))
    output = str(record.get("output", "")).strip() or "(no verifier output)"
    return (
        "Runtime verification failed. You have exactly one repair attempt. "
        "Inspect the failure, make the smallest correct fix, then call submit_final again.\n"
        f"Command: {command}\n"
        f"Exit code: {record.get('exit_code')}\n"
        f"Output:\n{output}"
    )


def _verified_change_final(agent, progress_tracker):
    """Build the final response from audited runtime verification evidence only."""
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


class AgentTurnState(TypedDict, total=False):
    """Transient routing state; durable audit/resume state stays in ``TaskState``."""

    prompt_snapshot: tuple[str, dict[str, Any]] | None
    finalization_attempts: int
    context_compaction_trigger: str
    compact_after_tool: bool
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


def _action_tool_name(definition):
    return str(definition["name"]).strip()


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
    hard_tool_limit = (
        agent.hard_max_steps if agent.feature_enabled("dynamic_budget") else agent.max_steps
    )
    task_state.configure_tool_budget(agent.max_steps, hard_tool_limit)
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

    max_retry_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
    max_graph_iterations = max(hard_tool_limit * 3, hard_tool_limit + 4)
    progress_tracker = {
        "last_workspace_change_step": 0,
        "repair_attempted": False,
        "last_runtime_verification": {},
    }
    model_metrics = _empty_model_metrics()
    # Scripted offline clients use the same flat Responses definitions as the
    # production client when they do not expose ``action_tools`` themselves.
    tool_builder = getattr(agent.model_client, "action_tools", toolkit.responses_action_tools)

    def finish_stopped():
        if task_state.attempts >= max_retry_attempts and task_state.tool_steps < hard_tool_limit:
            final = "Stopped after too many rejected model actions without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.mark_work_finished(final, stopped=True)
        _create_checkpoint(agent, task_state, user_message, task_state.stop_reason or "run_stopped")
        return _write_finished_run(
            agent, task_state, final, run_started_at, model_metrics
        )

    def finish_success(final, *, trigger):
        agent.mark_work_finished(final)
        task_state.finish_success(final)
        _create_checkpoint(agent, task_state, user_message, trigger)
        return _write_finished_run(
            agent, task_state, final, run_started_at, model_metrics
        )

    def finish_verification_failed(verification):
        command = str(verification.get("command", "verification command")).strip()
        final = (
            "Stopped after runtime verification failed following one repair attempt. "
            f"Command: {command}."
        )
        agent.mark_work_finished(final, stopped=True)
        task_state.stop_verification_failed(final)
        _create_checkpoint(agent, task_state, user_message, "runtime_verification_failed")
        return _write_finished_run(
            agent, task_state, final, run_started_at, model_metrics
        )

    def finish_verification_stale(verification):
        command = str(verification.get("command", "verification command")).strip()
        final = (
            "Stopped because the workspace changed after runtime verification passed. "
            f"Run {command} again against the current workspace."
        )
        agent.mark_work_finished(final, stopped=True)
        task_state.stop_verification_stale(final)
        _create_checkpoint(agent, task_state, user_message, "runtime_verification_stale")
        return _write_finished_run(
            agent, task_state, final, run_started_at, model_metrics
        )

    def call_model(state: AgentTurnState):
        if task_state.attempts >= max_retry_attempts:
            return Command(update={"result": finish_stopped()}, goto=END)

        finalization_only = task_state.tool_steps >= task_state.hard_tool_limit
        finalization_attempts = int(state.get("finalization_attempts", 0))
        if finalization_only:
            # A provider can choose a stale tool after the hard limit. Return
            # that rejection as a matching tool result, then give it one
            # bounded chance to submit the required final answer.
            if finalization_attempts >= 2:
                return Command(update={"result": finish_stopped()}, goto=END)
            finalization_attempts += 1

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
            )
            if budget_notice:
                prompt = f"{prompt}\n\n{budget_notice}"
            prompt_metadata["tool_budget"] = {
                "nominal": task_state.nominal_tool_budget,
                "hard_limit": task_state.hard_tool_limit,
            }
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
                "finalization_only": finalization_only,
                "hard_tool_limit": task_state.hard_tool_limit,
                "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                **agent.identity_metadata(),
            },
        )
        model_started_at = time.monotonic()
        try:
            action_tools = tool_builder(agent.tools)
            final_action_tools = [
                tool for tool in action_tools if _action_tool_name(tool) == "submit_final"
            ]
            action = agent.model_client.complete_action(
                prompt,
                agent.max_new_tokens,
                action_tools=final_action_tools if finalization_only else action_tools,
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
                        "finalization_attempts": finalization_attempts,
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

        if finalization_only and kind != ACTION_FINAL:
            finalization_reason = "the hard tool limit is reached"
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
            verification = _run_runtime_verification(agent, task_state)
            progress_tracker["last_runtime_verification"] = verification
            if agent.runtime_verification_is_current(verification):
                final = _verified_change_final(agent, progress_tracker)
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
            feedback = _verification_feedback(verification)
            _record_action_result(agent, action, feedback)
            agent.mark_retry_needed(feedback)
            _create_checkpoint(agent, task_state, user_message, "runtime_verification_failed")
            return Command(
                update={
                    "prompt_snapshot": prompt_snapshot,
                    "finalization_attempts": finalization_attempts,
                },
                goto="call_model",
            )

        next_state = {
            "prompt_snapshot": prompt_snapshot,
            "finalization_attempts": finalization_attempts,
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
            if agent.needs_context_compaction(
                task_state,
                completion_metadata.get("input_tokens"),
            ):
                return Command(
                    update={
                        **next_state,
                        "context_compaction_trigger": "input_threshold",
                    },
                    goto="compact_context",
                )
            return Command(update=next_state, goto="call_model")

        final = str(payload).strip()
        result = finish_success(final, trigger="run_end")
        return Command(update={"result": result}, goto=END)

    def execute_tool(state: AgentTurnState):
        _execute_tool_action(
            agent,
            task_state,
            user_message,
            state["action"],
            progress_tracker,
        )
        if state.get("compact_after_tool", False) and agent.can_compact_task_context(task_state):
            return Command(
                update={
                    "finalization_attempts": state.get("finalization_attempts", 0),
                    "context_compaction_trigger": "input_threshold",
                },
                goto="compact_context",
            )
        return Command(
            update={"prompt_snapshot": state.get("prompt_snapshot")},
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
                "finalization_attempts": state.get("finalization_attempts", 0),
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
            {"finalization_attempts": 0},
            config={"recursion_limit": max(32, max_graph_iterations * 4 + 16)},
        )
    return result["result"]
