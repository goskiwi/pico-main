"""The only model/tool loop in Pico."""

from __future__ import annotations

import json
import time
import uuid

from .changes import build_task_diff
from .completion import CompletionController
from .context_manager import ContextBudgetExceeded
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .outcome import RunOutcome
from .providers import ProviderContextOverflow
from .session import new_loop_control, new_verification, utc_now


class AgentLoop:
    def __init__(self, runtime):
        self.runtime = runtime
        self.completion = CompletionController(runtime)

    def run(self, user_message):  # noqa: C901 - one visible orchestration loop
        runtime = self.runtime
        session = runtime.session
        runtime.usage.reset()
        self._start_task(str(user_message))
        runtime.model_client.reset_action_session()
        execution = (
            runtime.parent_execution_context.child()
            if runtime.parent_execution_context is not None
            else ExecutionContext.root(max_seconds=runtime.config.turn_timeout_seconds)
        )
        runtime.execution_context = execution
        runtime.emit_trace("run_started", session_id=session.id, run_id=session.run["id"])
        runtime.current_memories = []
        started = time.monotonic()
        turns = tools = 0
        answer = stop_reason = ""
        status = "stopped"
        overflow_retried = False
        try:
            self._recall(user_message, execution)
            while turns < runtime.config.max_agent_turns:
                execution.check_active()
                # Every request sends the current Session projection in full.
                runtime.model_client.reset_action_session()
                surface = runtime.tools.resolve_surface()
                instructions, input_text = runtime.context.build(surface, execution)
                sent_end = len(session.history)
                turns += 1
                session.run["turns"] = turns
                session.save()
                runtime.emit_trace("model_requested", turn=turns)
                try:
                    action = runtime.model_client.complete_action(
                        input_text,
                        runtime.config.max_new_tokens,
                        instructions=instructions,
                        action_tools=surface.action_tools,
                        execution_context=execution.child(),
                    )
                except ProviderContextOverflow:
                    if overflow_retried:
                        raise
                    runtime.context.build(surface, execution, force=True)
                    overflow_retried = True
                    continue
                overflow_retried = False
                if action.kind != "invalid":
                    session.observed = sent_end
                session.run["usage"] = runtime.usage.snapshot()
                runtime.emit_trace(
                    "model_finished",
                    turn=turns,
                    action=action.kind,
                    usage=getattr(runtime.model_client, "last_completion_metadata", {}),
                )

                if action.kind == "tool":
                    _index, entry = session.begin_tool_turn(
                        action.tool_calls, runtime.redact_facts
                    )
                    session.save()
                    if runtime.context.refresh_rules():
                        runtime.tools.reject_group(entry, action.tool_calls)
                        continue
                    outcomes = runtime.tools.execute_group(
                        action.tool_calls, entry, execution.child(), surface
                    )
                    tools += len(outcomes)
                    runtime.emit_trace(
                        "tools_finished",
                        count=len(outcomes),
                        statuses=[outcome.status for outcome in outcomes],
                    )
                    session.run["tools"] = tools
                    session.loop_control["invalid_outputs"] = 0
                    session.save()
                    continue

                if action.kind == "invalid":
                    session.loop_control["invalid_outputs"] += 1
                    session.append_feedback(
                        "Invalid model output: " + str(action.content)
                    )
                    session.save()
                    runtime.model_client.reset_action_session()
                    if session.loop_control["invalid_outputs"] >= 8:
                        stop_reason = "invalid_output_limit"
                        break
                    continue

                session.loop_control["invalid_outputs"] = 0
                session.append_final(action.content)
                session.save()
                decision = self.completion.check(action.content, execution.child())
                if decision.allowed:
                    answer = decision.detail
                    status = "completed"
                    break
                session.loop_control["completion_blocks"] += 1
                session.loop_control["last_completion_error"] = decision.error
                session.append_feedback(
                    "Runtime completion check: "
                    + json.dumps(
                        {
                            "status": decision.status,
                            "error": decision.error,
                            "detail": decision.detail,
                            "data": decision.data,
                        },
                        ensure_ascii=False,
                    )
                )
                session.save()
                if decision.status == "stop":
                    answer, stop_reason = decision.detail, decision.error
                    break
                if session.loop_control["completion_blocks"] >= 3:
                    answer, stop_reason = decision.detail, "completion_block_limit"
                    break
                runtime.model_client.reset_action_session()
            else:
                stop_reason = "agent_turn_limit"
        except ExecutionCancelled as exc:
            stop_reason = str(exc) or "user_cancelled"
        except ExecutionDeadlineExceeded:
            stop_reason = "deadline_exceeded"
        except ContextBudgetExceeded as exc:
            stop_reason = "context_budget_exceeded: " + str(exc)
        except Exception as exc:  # noqa: BLE001 - persist a stopped Session on runtime failure
            stop_reason = runtime.redact_text(f"runtime_error: {type(exc).__name__}: {exc}")
        finally:
            session.recover()
            session.run.update(
                {
                    "status": status,
                    "stop_reason": stop_reason,
                    "turns": turns,
                    "tools": tools,
                    "ended_at": utc_now(),
                    "usage": runtime.usage.snapshot(),
                }
            )
            session.save()
            runtime.emit_trace("run_finished", status=status, stop_reason=stop_reason)
        if status != "completed":
            answer = "Task not completed: " + stop_reason + ("\n" + answer if answer else "")
        if session.unconfirmed:
            answer += "\n\nRemaining uncertainty: " + ", ".join(
                item["id"] for item in session.unconfirmed
            )
        memory_changes = []
        if status == "completed" and runtime.config.memory_enabled:
            try:
                memory_changes = runtime.memory.extract(
                    session, runtime.model_client, execution.child()
                )
            except Exception as exc:  # noqa: BLE001 - optional memory cannot undo completion
                runtime.emit_trace("memory_failed", error=runtime.redact_text(str(exc)))
                memory_changes = []
        session.run["usage"] = runtime.usage.snapshot()
        session.save()
        task_diff = build_task_diff(session, runtime.artifacts)
        return RunOutcome(
            session_id=session.id,
            status=status,
            answer=runtime.redact_text(answer),
            run_id=session.run["id"],
            stop_reason=stop_reason,
            verification=session.verification["status"],
            turns=turns,
            tools=tools,
            metrics={
                **session.run.get("usage", {}),
                "seconds": round(time.monotonic() - started, 3),
                "memory_changes": len(memory_changes),
            },
            task_diff=task_diff,
        )

    def _start_task(self, user_message):
        session = self.runtime.session
        config = self.runtime.config
        completed = session.run.get("status") in {"completed", "reset"}
        if completed:
            session.loop_control = new_loop_control()
            session.verification = new_verification()
            session.verification_required = False
            session.unconfirmed = []
            session.mutations = []
            session.file_states = {}
            session.task_policy = {}
        elif session.verification["status"] == "passed":
            session.verification.update(status="stale", workspace_state=None)
        current_paths = session.task_policy.get("write_paths", config.allowed_write_paths)
        requested_paths = config.allowed_write_paths
        if config.mode == "ask":
            effective_paths = []
        elif current_paths is None:
            effective_paths = None if requested_paths is None else list(requested_paths)
        elif requested_paths is None:
            effective_paths = list(current_paths)
        else:
            allowed = set(requested_paths)
            effective_paths = [path for path in current_paths if path in allowed]
        session.task_policy = {
            "write_paths": effective_paths,
            "verification_floor": bool(
                session.task_policy.get("verification_floor")
                or (config.verification_command and config.mode != "ask")
            ),
        }
        session.verification_required |= session.task_policy["verification_floor"]
        session.append_user(self.runtime.redact_text(user_message))
        session.run = {
            "id": uuid.uuid4().hex[:16],
            "status": "running",
            "started_at": utc_now(),
            "turns": 0,
            "tools": 0,
            "usage": self.runtime.usage.snapshot(),
        }
        session.save()

    def _recall(self, message, execution):
        if not self.runtime.config.memory_enabled:
            return
        try:
            self.runtime.current_memories = self.runtime.memory.recall(
                str(message), self.runtime.model_client, execution.child()
            )
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            raise
        except Exception as exc:  # noqa: BLE001 - optional recall is diagnosed
            self.runtime.emit_trace("memory_failed", error=self.runtime.redact_text(str(exc)))
