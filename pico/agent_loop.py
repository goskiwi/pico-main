"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_PARTIAL_STALE_STATUS,
    CHECKPOINT_WORKSPACE_MISMATCH_STATUS,
)
from .completion import CompletionGate
from .context_ledger import ContextLedger
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .hooks import AfterToolContext, TurnContext
from .task_state import TaskState
from .verification import changed_python_syntax_issues
from .workspace import clip, now


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.memory.set_goal(user_message)
        agent._task_memory_selection = None
        agent.evidence_ledger = type(agent.evidence_ledger)()

        checkpoint = agent.current_checkpoint()
        saved_task = dict((checkpoint or {}).get("task_state", {}) or {})
        can_resume = (
            agent.resume_state.get("status") == CHECKPOINT_FULL_VALID_STATUS
            and saved_task.get("status") == "running"
            and (checkpoint or {}).get("context_run_id")
        )
        if can_resume:
            prior_events = agent.run_store.read_events(checkpoint["context_run_id"])
            task_state = TaskState.from_dict(
                agent.run_store.replay(checkpoint["context_run_id"]).task_state(saved_task)
            )
            ledger = ContextLedger.restore(checkpoint["context_run_id"], agent.run_store)
            ledger.append_guidance(f"Resume request: {user_message}")
            agent.evidence_ledger = type(agent.evidence_ledger).from_events(prior_events)
        else:
            task_state = TaskState.create(run_id=agent.new_run_id(), task_id=agent.new_task_id(), user_request=user_message)
            ledger = None
            prior_events = []
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_execution = ExecutionContext.root(
            run_id=task_state.run_id,
            task_id=task_state.task_id,
            owner="agent_loop",
            max_seconds=agent.run_timeout_seconds,
        )
        agent.current_run_dir = agent.run_store.start_run(task_state)
        if ledger is None:
            ledger = ContextLedger(task_state.run_id, agent.run_store)
            ledger.append_user(user_message)
        agent.context_ledger = ledger
        context_generation = ledger.generation
        agent.record({"role": "user", "content": user_message, "created_at": now()})
        completion_gate = CompletionGate()
        completion_gate.restore_partial_paths((checkpoint or {}).get("pending_partial_paths", []) if can_resume else [])
        completion_gate.restore_partial_paths(
            path
            for entry in ledger.active_entries()
            if entry.kind == "tool_result"
            and (entry.outcome_status == "partial_success" or entry.side_effect_state == "unknown")
            for path in (entry.affected_paths or (f"operation:{entry.call_id}",))
        )
        agent.emit_event(
            task_state,
            "run_resumed" if can_resume else "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )
        for outcome in ledger.reconciled_outcomes:
            task_state.record_tool(outcome.tool_name)
            completion_gate.observe(outcome)
            content_fingerprint = agent.content_workspace_fingerprint()
            agent.evidence_ledger.record_tool(outcome, content_fingerprint)
            agent.emit_event(
                task_state,
                "operation_finished",
                {
                    "tool_call_id": outcome.tool_call_id,
                    "tool_name": outcome.tool_name,
                    "content_workspace_fingerprint": content_fingerprint,
                    "recovered_from_interruption": True,
                    "outcome": outcome.to_dict(),
                },
                correlation_id=outcome.tool_call_id,
            )
        if ledger.reconciled_outcomes:
            agent.run_store.write_task_state(task_state)

        agent.model_client.reset_action_session()
        prompt_snapshot = None

        tool_steps = task_state.tool_steps
        attempts = task_state.attempts
        malformed_retries = 0
        execution_stop = ""

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：首次或 provider session 重置后组装 prompt
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / event log / memory
        # 然后进入下一轮，直到停机条件满足
        # A tool budget of N permits at most N executions plus one final-only
        # model turn. Without that grace turn, a successful Nth tool result can
        # never be converted into a final answer.
        while True:
            try:
                agent.current_execution.check_active()
            except ExecutionDeadlineExceeded:
                execution_stop = "deadline_exceeded"
                break
            except ExecutionCancelled as exc:
                execution_stop = str(exc) or "user_cancelled"
                break
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt_reused = prompt_snapshot is not None
            if prompt_snapshot is None:
                prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
                prompt_snapshot = (prompt, dict(prompt_metadata))
            else:
                prompt, original_metadata = prompt_snapshot
                prompt_metadata = dict(original_metadata)
            prompt_metadata["prompt_reused"] = prompt_reused
            prompt_metadata["provider_session_active"] = prompt_reused
            next_generation = int(prompt_metadata.get("ledger_generation", ledger.generation))
            if not prompt_reused and next_generation > context_generation:
                context_generation = next_generation
                agent.emit_event(
                    task_state,
                    "context_folded",
                    {"generation": context_generation},
                )
            memory_audit = dict(prompt_metadata.get("memory_retrieval", {}) or {})
            if not prompt_reused and (
                memory_audit.get("available_count") or memory_audit.get("selected_filenames")
            ):
                agent.emit_event(task_state, "memory_selection", memory_audit)
            agent.emit_event(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if not prompt_reused and prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_event(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif not prompt_reused and prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_event(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_event(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if not prompt_reused and prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_event(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            agent.emit_event(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            if getattr(agent.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            model_started_at = time.monotonic()
            action_tools = (
                [tool for tool in agent.action_tools if tool["name"] == "submit_final"]
                if agent.max_steps is not None and tool_steps >= agent.max_steps
                else agent.action_tools
            )
            action = agent.model_client.complete_action(
                prompt,
                agent.max_new_tokens,
                action_tools=action_tools,
                prompt_cache_key=prompt_cache_key,
                request_timeout=agent.current_execution.bounded_timeout(),
            )
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 Runtime events。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            agent.emit_event(
                task_state,
                "model_parsed",
                {
                    "kind": action.kind,
                    "tool_call_id": action.tool_call.call_id if action.tool_call else "",
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )
            provider_input_tokens = completion_metadata.get("input_tokens")
            reset_provider_session = (
                isinstance(provider_input_tokens, int)
                and provider_input_tokens + agent.max_new_tokens
                >= agent.provider_context_limit_tokens
            )

            def continue_provider(
                feedback,
                *,
                tool_call_id="",
                should_reset=reset_provider_session,
                input_tokens=provider_input_tokens,
                current_action=action,
            ):
                nonlocal prompt_snapshot
                if should_reset:
                    agent.model_client.reset_action_session()
                    prompt_snapshot = None
                    agent.emit_event(
                        task_state,
                        "provider_session_reset",
                        {
                            "reason": "input_threshold",
                            "input_tokens": input_tokens,
                            "tool_call_id": tool_call_id,
                        },
                        correlation_id=tool_call_id,
                    )
                    return
                agent.model_client.record_action_result(current_action, feedback)

            if action.kind == "tool":
                if agent.max_steps is not None and tool_steps >= agent.max_steps:
                    break
                malformed_retries = 0
                call = action.tool_call
                name, args = call.name, call.args
                ledger.append_tool_call(call)
                outcome = agent.run_tool(call)
                if outcome.execution_state != "not_started":
                    tool_steps += 1
                    task_state.record_tool(name)
                completion_gate.observe(outcome)
                context_result = ledger.append_tool_result(outcome)
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": context_result.content,
                        "artifact_id": context_result.artifact_id,
                        "content_tier": context_result.content_tier,
                        "original_size_bytes": context_result.original_size_bytes,
                        "created_at": now(),
                    }
                )
                hook_decision = agent.hooks.after_tool_result(
                    AfterToolContext(
                        outcome=outcome,
                        tool_steps=tool_steps,
                        run_id=task_state.run_id,
                        task_id=task_state.task_id,
                    )
                )
                turn_decision = agent.hooks.should_stop_after_turn(
                    TurnContext(
                        action_kind="tool",
                        tool_steps=tool_steps,
                        attempts=attempts,
                        run_id=task_state.run_id,
                        task_id=task_state.task_id,
                    )
                )
                guidance = "\n".join(
                    part for part in (hook_decision.guidance, turn_decision.guidance) if part
                )
                if guidance:
                    ledger.append_guidance(guidance)
                    agent.record({"role": "assistant", "content": guidance, "created_at": now()})
                if agent.max_steps is not None and tool_steps >= agent.max_steps:
                    budget_guidance = (
                        "Runtime tool budget exhausted. Do not call another tool; "
                        "use submit_final now with the available evidence."
                    )
                    ledger.append_guidance(budget_guidance)
                    agent.record({"role": "assistant", "content": budget_guidance, "created_at": now()})
                    guidance = "\n".join(part for part in (guidance, budget_guidance) if part)
                provider_result = outcome.content
                if guidance:
                    provider_result += "\n\nRuntime guidance: " + guidance
                continue_provider(provider_result, tool_call_id=call.call_id)
                policy_stop = (
                    hook_decision.stop
                    or turn_decision.stop
                    or bool(outcome.metadata.get("policy_stop_requested"))
                )
                if hook_decision.active or turn_decision.active or policy_stop:
                    reason = " | ".join(
                        part for part in (
                            hook_decision.reason,
                            turn_decision.reason,
                            outcome.failure.detail
                            if outcome.metadata.get("policy_stop_requested") and outcome.failure
                            else "",
                        ) if part
                    )
                    agent.emit_event(
                        task_state,
                        "policy_decided",
                        {
                            "stop": bool(policy_stop),
                            "reason": reason,
                            "guidance": guidance,
                            "tool_call_id": call.call_id,
                        },
                        correlation_id=call.call_id,
                    )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="tool_executed")
                agent.run_store.write_task_state(task_state)
                agent.emit_event(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
                if policy_stop:
                    execution_stop = "policy:" + (reason or "runtime policy requested stop")
                    break
                continue

            if action.kind == "retry":
                malformed_retries += 1
                ledger.append_guidance(action.content)
                agent.record({"role": "assistant", "content": action.content, "created_at": now()})
                retry_notice = action.content
                continue_provider(retry_notice)
                agent.run_store.write_task_state(task_state)
                if malformed_retries >= 8:
                    execution_stop = "malformed_model_retry_limit"
                    break
                continue

            final = action.content.strip()
            syntax_issues = changed_python_syntax_issues(agent)
            if syntax_issues:
                guidance = "Runtime completion gate: changed Python is invalid: " + "; ".join(syntax_issues)
                ledger.append_guidance(guidance)
                agent.record({"role": "assistant", "content": guidance, "created_at": now()})
                agent.emit_event(task_state, "completion_blocked", {"status": "syntax_invalid", "reason": guidance})
                continue_provider(guidance)
                continue
            preliminary = completion_gate.assess()
            if (agent.evidence_ledger.changed_paths or not preliminary.allowed) and agent.verification_command:
                fingerprint = agent.content_workspace_fingerprint()
                verification = agent.evidence_ledger.current_verification(fingerprint)
                if verification is None:
                    agent.emit_event(task_state, "verification_started", {"command": agent.verification_command})
                    verification = agent.run_verification()
                    agent.emit_event(task_state, "verification_finished", verification or {"status": "skipped"})
                if not verification or verification.get("status") != "passed":
                    guidance = (
                        "Runtime verification failed; inspect and repair before submit_final.\n"
                        + str((verification or {}).get("output", "verification unavailable"))
                    )
                    ledger.append_guidance(guidance)
                    agent.record({"role": "assistant", "content": guidance, "created_at": now()})
                    agent.emit_event(task_state, "completion_blocked", {"status": "verification_failed", "reason": guidance})
                    continue_provider(guidance)
                    continue
                completion_gate.observe_verification(True)
            decision = completion_gate.assess()
            if not decision.allowed:
                guidance = f"Runtime completion gate: {decision.reason}. Inspect or repair before returning a final answer."
                ledger.append_guidance(guidance)
                agent.record({"role": "assistant", "content": guidance, "created_at": now()})
                agent.emit_event(task_state, "completion_blocked", {"status": decision.status, "reason": decision.reason})
                continue_provider(guidance)
                continue
            ledger.append_final(final)
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
            agent.run_store.write_task_state(task_state)
            agent.emit_event(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
                },
            )
            agent.emit_event(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            agent.current_execution.transition("completed")
            agent.current_execution = None
            return final

        if execution_stop.startswith("policy:"):
            reason = execution_stop.removeprefix("policy:")
            final = f"Stopped by runtime policy: {reason}."
            task_state.stop("policy_stop", final_answer=final)
        elif execution_stop == "malformed_model_retry_limit":
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        elif execution_stop:
            final = f"Stopped because execution was interrupted: {execution_stop}."
            task_state.stop(execution_stop, final_answer=final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_event(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        agent.emit_event(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        if agent.current_execution is not None:
            agent.current_execution.transition(
                "cancelled" if execution_stop and execution_stop != "deadline_exceeded" else (
                    "timed_out" if execution_stop else "completed"
                ),
                stop_reason=execution_stop,
            )
            agent.current_execution = None
        return final
