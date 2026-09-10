"""The only model/tool loop in Pico."""

from __future__ import annotations

import json
import time

from .changes import build_final_diff
from .completion import CompletionController
from .context_manager import ContextBudgetExceeded
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .outcome import RunOutcome
from .providers import ProviderContextOverflow
from .session import new_loop_control, new_run_id, new_verification, utc_now


class AgentLoop:
    def __init__(self, runtime):
        self.runtime = runtime
        self.completion = CompletionController(runtime)

    def run(self, user_message):
        runtime = self.runtime
        session = runtime.session
        runtime.usage.reset()
        self._start_task(str(user_message))
        execution = self._new_execution()
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
                surface, instructions, input_text, sent_end = self._prepare_model_input(
                    execution
                )
                turns += 1
                try:
                    action = self._request_action(
                        surface, instructions, input_text, sent_end, turns, execution
                    )
                except ProviderContextOverflow:
                    if overflow_retried:
                        raise
                    runtime.context.build(surface, execution, force=True)
                    overflow_retried = True
                    continue
                overflow_retried = False

                if action.kind == "tool":
                    tools += self._handle_tool_calls(action, surface, execution)
                    session.run["tools"] = tools
                    session.save()
                    continue
                if action.kind == "invalid":
                    stop_reason = self._handle_invalid_action(action)
                    if stop_reason:
                        break
                    continue

                completion = self._handle_completion_request(action, execution)
                if completion is None:
                    continue
                status, answer, stop_reason = completion
                break
            else:
                stop_reason = "agent_turn_limit"
        except ExecutionCancelled as exc:
            stop_reason = str(exc) or "user_cancelled"
        except ExecutionDeadlineExceeded:
            stop_reason = "deadline_exceeded"
        except ContextBudgetExceeded as exc:
            stop_reason = "context_budget_exceeded: " + str(exc)
        except Exception as exc:  # noqa: BLE001 - persist a stopped Session on runtime failure
            stop_reason = runtime.redact_text(
                f"runtime_error: {type(exc).__name__}: {exc}"
            )
        finally:
            self._record_run_end(status, stop_reason, turns, tools)
        return self._finish_run(
            status, answer, stop_reason, turns, tools, started, execution
        )

    def _new_execution(self):
        parent = self.runtime.parent_execution_context
        if parent is not None:
            return parent.child()
        return ExecutionContext.root(
            max_seconds=self.runtime.config.turn_timeout_seconds
        )

    def _prepare_model_input(self, execution):
        runtime = self.runtime
        surface = runtime.tools.resolve_surface()
        instructions, input_text = runtime.context.build(surface, execution)
        return surface, instructions, input_text, len(runtime.session.history)

    def _request_action(
        self, surface, instructions, input_text, sent_end, turn, execution
    ):
        runtime = self.runtime
        session = runtime.session
        session.run["turns"] = turn
        session.save()
        runtime.emit_trace("model_requested", turn=turn)
        action = runtime.model_client.complete_action(
            input_text,
            runtime.config.max_new_tokens,
            instructions=instructions,
            action_tools=surface.action_tools,
            execution_context=execution.child(),
        )
        if action.kind != "invalid":
            session.observed = sent_end
        session.run["usage"] = runtime.usage.snapshot()
        runtime.emit_trace(
            "model_finished",
            turn=turn,
            action=action.kind,
            usage=getattr(runtime.model_client, "last_completion_metadata", {}),
        )
        return action

    def _handle_tool_calls(self, action, surface, execution):
        runtime = self.runtime
        session = runtime.session
        _index, entry = session.begin_tool_turn(
            action.tool_calls, runtime.redact_facts
        )
        session.save()
        if runtime.context.refresh_rules():
            runtime.tools.reject_group(entry, action.tool_calls)
            return 0
        outcomes = runtime.tools.execute_group(
            action.tool_calls, entry, execution.child(), surface
        )
        runtime.emit_trace(
            "tools_finished",
            count=len(outcomes),
            statuses=[outcome.status for outcome in outcomes],
        )
        session.loop_control["invalid_outputs"] = 0
        session.save()
        return len(outcomes)

    def _handle_invalid_action(self, action):
        runtime = self.runtime
        session = runtime.session
        session.loop_control["invalid_outputs"] += 1
        session.append_feedback("Invalid model output: " + str(action.content))
        session.save()
        if session.loop_control["invalid_outputs"] >= 8:
            return "invalid_output_limit"
        return ""

    def _handle_completion_request(self, action, execution):
        runtime = self.runtime
        session = runtime.session
        session.loop_control["invalid_outputs"] = 0
        session.append_final(action.content)
        session.save()
        decision = self.completion.check(action.content, execution.child())
        if decision.allowed:
            return "completed", decision.detail, ""
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
            return "stopped", decision.detail, decision.error
        if session.loop_control["completion_blocks"] >= 3:
            return "stopped", decision.detail, "completion_block_limit"
        return None

    def _record_run_end(self, status, stop_reason, turns, tools):
        runtime = self.runtime
        session = runtime.session
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

    def _finish_run(
        self, status, answer, stop_reason, turns, tools, started, execution
    ):
        runtime = self.runtime
        session = runtime.session
        if status != "completed":
            answer = "Task not completed: " + stop_reason + (
                "\n" + answer if answer else ""
            )
        if session.unconfirmed:
            answer += "\n\nRemaining uncertainty: " + ", ".join(
                item["id"] for item in session.unconfirmed
            )
        memory_changes = []
        if status == "completed" and runtime.parent_execution_context is None:
            try:
                memory_changes = runtime.memory.extract(
                    session, runtime.model_client, execution.child()
                )
            except Exception as exc:  # noqa: BLE001 - optional memory cannot undo completion
                runtime.emit_trace(
                    "memory_failed", error=runtime.redact_text(str(exc))
                )
        session.run["usage"] = runtime.usage.snapshot()
        session.save()
        final_diff = build_final_diff(session, runtime.artifacts)
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
            final_diff=final_diff,
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
            "id": new_run_id(),
            "status": "running",
            "started_at": utc_now(),
            "turns": 0,
            "tools": 0,
            "usage": self.runtime.usage.snapshot(),
        }
        session.save()

    def _recall(self, message, execution):
        if self.runtime.parent_execution_context is not None:
            return
        try:
            self.runtime.current_memories = self.runtime.memory.recall(
                str(message), self.runtime.model_client, execution.child()
            )
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            raise
        except Exception as exc:  # noqa: BLE001 - optional recall is diagnosed
            self.runtime.emit_trace("memory_failed", error=self.runtime.redact_text(str(exc)))
