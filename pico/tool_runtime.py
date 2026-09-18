"""The one public runtime boundary for model-visible tools."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from . import tools as toolkit
from .artifacts import head_tail
from .contracts import (
    TOOL_OUTPUT_MAX_BYTES,
    FailureInfo,
    ToolCall,
    ToolExecutionPlan,
    ToolFailureError,
    ToolOutcome,
    ToolRunnerResult,
)
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .security import redact_facts
from .tool_context import ToolContext
from .tool_execution import (
    classify_runner_result,
    effect_diff,
    intersect_write_scopes,
)
from .workspace import clip

if TYPE_CHECKING:
    from .runtime import Pico

ASK_TOOL_NAMES = frozenset(
    {
        "list_files",
        "read_file",
        "read_artifact",
        "read_history",
        "list_memories",
        "read_memory",
        "search",
        "submit_final",
    }
)
EFFECT_SETTLEMENT_TIMEOUT_SECONDS = 30


@dataclass(slots=True)
class ResolvedToolSurface:
    """The one tool authority shared by a model turn and local execution."""

    mode: str
    allowed_write_paths: tuple[str, ...] | None
    definitions: dict[str, dict]
    action_tools: tuple[dict, ...]

    @property
    def names(self):
        return tuple(str(tool["name"]) for tool in self.action_tools)

    @property
    def policy(self):
        return self.mode, self.allowed_write_paths


def _run_id(agent):
    return ToolRuntime._recorded_run_log(agent).run_id


class ToolRuntime:
    """Own tool discovery, policy admission, and transaction execution."""

    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.registry = self._build_registry()
        self._validate_allowlist(self.registry)

    def reconcile_interrupted(self):
        """Close one interrupted Tool transaction without replaying its Runner."""

        runtime = self.runtime
        run_log = runtime.run.run_log
        if run_log is None:
            return None
        pending = run_log.projection.pending_tool
        if pending is None:
            self.close_unstarted_calls("operation_interrupted")
            return None
        call = pending.call
        observation_context = ExecutionContext.standalone(
            max_seconds=EFFECT_SETTLEMENT_TIMEOUT_SECONDS
        )
        potential = list(pending.potential_effects)
        effects_before = {}
        effects_after = {}
        for effect in potential:
            logical = str(effect.get("path", ""))
            if not logical:
                continue
            path = Path(logical)
            if not path.is_absolute():
                path = runtime.workspace.resolve_path(logical)
            before = str(effect.get("before_state", ""))
            after = runtime.workspace.path_state(
                path,
                execution_context=observation_context,
            )
            effects_before[logical] = before
            effects_after[logical] = after
        changed = effect_diff(effects_before, effects_after)
        effect_scope = pending.effect_scope
        untracked = effect_scope == "workspace" and not potential
        uncertain = bool(changed or untracked)
        if untracked:
            interruption_detail = (
                "Tool execution was interrupted before a durable settlement. "
                "Workspace changes were not tracked; inspect the current "
                "workspace before deciding whether to retry."
            )
        elif changed:
            interruption_detail = (
                "Tool execution was interrupted before a durable settlement. "
                f"Observed changed paths: {', '.join(changed)}. Read their current "
                "contents before deciding whether to repair or continue."
            )
        else:
            interruption_detail = (
                "Tool execution was interrupted before a durable settlement. "
                "No tracked path changes were observed."
            )
        outcome = ToolOutcome(
            tool_call_id=call.call_id,
            tool_name=call.name,
            status="partial_success" if uncertain else "error",
            execution_state="failed",
            side_effect_state=(
                "partial" if changed else ("untracked" if untracked else "none")
            ),
            content="",
            failure=FailureInfo(
                "operation_interrupted",
                interruption_detail,
                "no_retry" if uncertain else "retry_after_change",
            ),
            affected_paths=tuple(changed),
            structured=self._revision_details(
                effects_before,
                effects_after,
                changed,
            ),
        )
        outcome = self.prepare_outcome(outcome)
        entry = run_log.append_tool_result(
            outcome,
            recovered_from_interruption=True,
        )
        self.close_unstarted_calls("operation_interrupted")
        return outcome, entry

    def close_unstarted_calls(self, reason="operation_interrupted"):
        """Close the unexecuted suffix of one interrupted Assistant turn."""

        run_log = self.runtime.run.run_log
        if run_log is None or run_log.projection.pending_tool is not None:
            return ()
        outcomes = []
        while run_log.projection.active_tool_turn is not None:
            call = run_log.projection.active_tool_turn.next_call
            outcome = self._outcome(
                call,
                "rejected",
                "not_started",
                "none",
                "",
                failure=FailureInfo(
                    str(reason),
                    "Tool Call was not started because its Assistant turn was interrupted.",
                    "retry_after_change",
                ),
            )
            run_log.append_tool_result(outcome)
            outcomes.append(outcome)
        return tuple(outcomes)

    def _build_registry(self):
        runtime = self.runtime
        return toolkit.build_tool_registry(
            workspace_root=runtime.workspace.root,
            path_resolver=runtime.workspace.resolve_tool_path,
            artifact_store=runtime.dependencies.artifacts,
            redact_text=runtime.redact_text,
            mutation_service=runtime.dependencies.mutations,
            command_runner=runtime.dependencies.command_runner,
            run_store=runtime.dependencies.run_store,
            memory_store=runtime.dependencies.memory_store,
        )

    def _validate_allowlist(self, tools):
        allowed_tools = self.runtime.config.allowed_tools
        if allowed_tools is None:
            return
        unknown = [name for name in allowed_tools if name not in tools]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")

    def _validate_args(self, name, args, tool, context):
        validated = tool["args_schema"].model_validate(args or {}).model_dump()
        context.execution_plan = self._plan_execution(tool, context, validated)
        validator = tool.get("validate")
        if validator is not None:
            validated = validator(context, validated)
        return validated

    def _effective_policy(self):
        run_log = self.runtime.run.run_log
        contract = run_log.projection.contract if run_log is not None else None
        configured_mode = self.runtime.config.mode
        mode_rank = {"ask": 0, "code": 1, "auto": 2}
        mode = (
            min(
                (configured_mode, contract.mode),
                key=mode_rank.__getitem__,
            )
            if contract is not None
            else configured_mode
        )
        if contract is not None and contract.write_scope.mode == "none":
            mode = "ask"
        paths = intersect_write_scopes(
            contract.write_scope.allowed_paths() if contract is not None else None,
            self.runtime.config.allowed_write_paths,
        )
        if paths == ():
            mode = "ask"
        return mode, (() if mode == "ask" else paths)

    @staticmethod
    def _require_write_scope(paths, allowed_paths):
        if allowed_paths is None:
            return
        outside = sorted(set(paths) - set(allowed_paths))
        if outside:
            raise ValueError("write path outside allowed scope: " + ", ".join(outside))

    @staticmethod
    def _tool_allowed_by_mode(name, mode):
        if mode == "ask":
            return name in ASK_TOOL_NAMES
        return True

    def resolve_surface(self):
        """Resolve one authoritative Tool set for advertisement and execution."""

        mode, allowed_write_paths = self._effective_policy()
        definitions = {}
        configured = self.runtime.config.allowed_tools
        contract = self.runtime.run.projection.contract
        for name, tool in self.registry.items():
            if configured is not None and name not in configured:
                continue
            if contract is not None and name not in contract.allowed_tools:
                continue
            if not self._tool_allowed_by_mode(name, mode):
                continue
            # Freeze this turn's definition membership and callbacks without
            # coupling execution to later Registry dictionary mutations.
            definitions[name] = dict(tool)

        action_tools = tuple(toolkit.build_action_tools(definitions))
        return ResolvedToolSurface(
            mode=mode,
            allowed_write_paths=allowed_write_paths,
            definitions=definitions,
            action_tools=action_tools,
        )

    def context(self, *, call_id, execution_context=None):
        runtime = self.runtime
        return ToolContext(
            run_id=_run_id(runtime),
            tool_call_id=str(call_id),
            execution_context=(
                execution_context
                if execution_context is not None
                else runtime.run.execution_context
            ),
        )

    def _resolve_tool(self, call, surface):
        tool = surface.definitions.get(call.name)
        if tool is not None:
            return tool, None
        if call.name not in self.registry:
            return None, self._rejected(
                call, "unknown_tool", "unknown tool", "retry_after_change",
            )
        return None, self._rejected(
            call,
            "tool_not_allowed",
            f"{call.name} is unavailable in {surface.mode} mode",
            "no_retry",
        )

    @staticmethod
    def _recorded_run_log(agent, call_id=None):
        run_log = agent.run.run_log
        if run_log is None:
            raise RuntimeError("tool execution requires an active Run")
        if call_id is None:
            return run_log
        pending = run_log.projection.pending_tool
        if pending is None:
            raise RuntimeError("active Run has no started tool")
        if str(call_id) != pending.call.call_id:
            raise RuntimeError("tool execution does not match the pending Run call")
        return run_log

    @classmethod
    def _record_tool_started(cls, agent, call, *, plan, potential_effects):
        run_log = cls._recorded_run_log(agent)
        return run_log.append_tool_started(
            call.call_id,
            effect_scope=plan.effect_scope,
            potential_effects=redact_facts(
                potential_effects, agent.redact_text
            ),
            operation=redact_facts(plan.operation, agent.redact_text),
        )

    @classmethod
    def _record_tool_result(cls, agent, outcome):
        run_log = cls._recorded_run_log(agent, outcome.tool_call_id)
        return run_log.append_tool_result(outcome)

    @staticmethod
    def _plan_execution(tool, context, args):
        planner = tool.get("plan")
        if planner is not None:
            plan = planner(context, args)
            if not isinstance(plan, ToolExecutionPlan):
                raise TypeError("tool planner must return ToolExecutionPlan")
            return plan
        return ToolExecutionPlan("none")

    @staticmethod
    def _effect_snapshot(agent, paths, *, settling=False):
        if not paths:
            return {}
        execution_context = (
            ExecutionContext.standalone(
                max_seconds=EFFECT_SETTLEMENT_TIMEOUT_SECONDS
            )
            if settling
            else agent.run.execution_context
        )
        if execution_context is None:
            raise RuntimeError(
                "workspace effect observation requires an active ExecutionContext"
            )
        return {
            logical: agent.workspace.path_state(
                path,
                execution_context=execution_context,
            )
            for logical, path in paths
        }

    def _validate_call(self, call, tool, context):
        try:
            args = self._validate_args(
                call.name, call.args, tool, context
            )
        except ToolFailureError as exc:
            return None, self._rejected(
                call,
                exc.failure.code,
                exc.failure.detail,
                exc.failure.recovery,
                structured=exc.structured,
            )
        except Exception as exc:  # noqa: BLE001 - validator boundary
            return None, self._rejected(
                call,
                "invalid_arguments",
                str(exc),
                "retry_after_change",
            )
        return ToolCall(call.name, args, call.call_id), None

    @staticmethod
    def _invoke_runner(tool, context, args):
        execution = context.execution_context
        if execution is not None:
            execution.check_active()
        result = tool["run"](context, args)
        if not isinstance(result, ToolRunnerResult):
            raise TypeError("tool runner must return ToolRunnerResult")
        return result

    def _result_outcome(self, call, result):
        status, side_effect, paths = classify_runner_result(
            result.failure,
            result.affected_paths,
            result.effect_scope,
        )
        return self._outcome(
            call,
            status,
            "completed",
            side_effect,
            result.content,
            failure=result.failure,
            affected_paths=paths,
            structured=result.structured,
            artifact_id=result.artifact_id,
        )

    @staticmethod
    def _revision_details(before, after, paths):
        if len(paths) != 1:
            return {}
        path = paths[0]
        return {
            "path": path,
            "before_revision": before[path],
            "after_revision": after[path],
        }

    def _observed_exception_outcome(
        self,
        call,
        error,
        *,
        effects_before,
        effects_after,
    ):
        typed = error if isinstance(error, ToolFailureError) else None
        if typed is not None:
            # ToolFailureError is the mutation boundary's pre-effect contract.
            # A changed file in this case is external drift, not a Tool effect.
            return self._outcome(
                call,
                "error",
                "failed",
                "none",
                "",
                failure=typed.failure,
                structured=typed.structured,
            )
        paths = effect_diff(effects_before, effects_after)
        uncertain = bool(paths)
        return self._outcome(
            call,
            "partial_success" if uncertain else "error",
            "failed",
            "partial" if uncertain else "none",
            "",
            failure=FailureInfo(
                "tool_partial_success" if uncertain else "tool_failed",
                str(error),
                "no_retry" if uncertain else "retry_after_change",
            ),
            affected_paths=paths,
            structured=self._revision_details(
                effects_before,
                effects_after,
                paths,
            ),
        )

    def _execute_prepared(
        self,
        call,
        tool,
        context,
        plan,
        *,
        effects_before,
    ):
        """Execute one admitted call and commit exactly one terminal fact."""

        agent = self.runtime
        workspace_effect = plan.effect_scope == "workspace"
        if workspace_effect:
            self._record_tool_started(
                agent,
                call,
                plan=plan,
                potential_effects=[
                    {
                        "path": path,
                        "before_state": state,
                    }
                    for path, state in sorted(effects_before.items())
                ],
            )
        context.execution_plan = plan
        try:
            result = self._invoke_runner(tool, context, call.args)
            outcome = self._result_outcome(call, result)
        except Exception as exc:  # noqa: BLE001 - tool boundary
            if plan.paths:
                effects_after = self._effect_snapshot(
                    agent,
                    plan.paths,
                    settling=True,
                )
                outcome = self._observed_exception_outcome(
                    call,
                    exc,
                    effects_before=effects_before,
                    effects_after=effects_after,
                )
            else:
                typed_error = exc if isinstance(exc, ToolFailureError) else None
                untracked = bool(not typed_error and workspace_effect)
                outcome = self._outcome(
                    call,
                    "partial_success" if untracked else "error",
                    "failed",
                    "untracked" if untracked else "none",
                    "",
                    failure=(typed_error.failure if typed_error else None)
                    or FailureInfo(
                        "tool_effect_untracked" if untracked else "tool_failed",
                        (
                            f"{exc}. Workspace changes were not tracked; inspect "
                            "the current workspace before deciding whether to retry."
                            if untracked
                            else str(exc)
                        ),
                        "no_retry" if untracked else "retry_after_change",
                    ),
                    structured=(
                        typed_error.structured
                        if typed_error
                        else {}
                    ),
                )
        if workspace_effect:
            self._record_tool_result(agent, outcome)
        else:
            agent.run.run_log.append_tool_result(outcome)
        return outcome

    def execute_call(self, call, surface):
        if not isinstance(surface, ResolvedToolSurface):
            raise TypeError("tool execution requires its resolved Tool surface")
        if not isinstance(call, ToolCall):
            raise TypeError("tool execution requires a ToolCall")
        runtime = self.runtime
        if runtime.run.resumable:
            raise RuntimeError("a dormant Run must be resumed before tool execution")
        if runtime.run.run_log is None:
            raise RuntimeError("tool execution requires an active Run")
        if runtime.run.projection.terminal:
            raise RuntimeError("terminal Run cannot execute a tool call")
        if runtime.run.projection.pending_tool is not None:
            raise RuntimeError("a tool is already started")
        active_turn = runtime.run.projection.active_tool_turn
        if active_turn is None or active_turn.next_call is None:
            raise RuntimeError("tool execution requires an active Assistant turn")
        if (call.call_id, call.name) != (
            active_turn.next_call.call_id,
            active_turn.next_call.name,
        ):
            raise RuntimeError("tool execution does not match the next Tool Call")
        return self._execute(call, surface)

    def _check_plan_scope(self, call, plan, allowed_paths):
        self._require_write_scope((path for path, _target in plan.paths), allowed_paths)

    def _approval_failure(self, name, args, surface, plan):
        if surface.mode == "auto" and name != "run_shell":
            return None
        handler = self.runtime.dependencies.approval_handler
        if handler is None:
            return FailureInfo("approval_denied", "approval denied", "no_retry")
        try:
            approved = bool(handler(name, deepcopy(args), deepcopy(plan)))
        except Exception as exc:  # noqa: BLE001 - host approval boundary
            return FailureInfo(
                "approval_failed",
                f"approval handler failed: {exc}",
                "user_action_required",
            )
        return (
            None
            if approved
            else FailureInfo("approval_denied", "approval denied", "no_retry")
        )

    def _execute_edit(self, call, tool, context, plan):
        agent = self.runtime
        logical, path = plan.paths[0]
        try:
            prepared = agent.dependencies.mutations.prepare_edit(
                path,
                call.args.get("old_text", ""),
                call.args["new_text"],
                execution_context=context.execution_context,
            )
            before = {logical: prepared.receipt.before_revision}
            context.execution_context.check_active()
        except ToolFailureError as exc:
            return self._rejected(call, exc.failure.code, exc.failure.detail,
                                  exc.failure.recovery, structured=exc.structured)
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            raise
        except Exception as exc:  # noqa: BLE001 - mutation planning boundary
            return self._rejected(call, "effect_planning_failed", str(exc), "retry_after_change")

        bound = {
            **tool,
            "run": partial(tool["run"], prepared=prepared),
        }
        return self._execute_prepared(
            call,
            bound,
            context,
            plan,
            effects_before=before,
        )

    def _execute(self, call, surface):
        agent = self.runtime
        name, args = call.name, call.args
        if name == "submit_final":
            return self._rejected(
                call,
                "final_call_must_be_alone",
                "submit_final must be the only call in its model response",
                "retry_after_change",
            )
        tool, admission_rejection = self._resolve_tool(call, surface)
        if admission_rejection is not None:
            return admission_rejection
        context = self.context(call_id=call.call_id)
        call, validation_rejection = self._validate_call(
            call, tool, context
        )
        if validation_rejection is not None:
            return validation_rejection
        args = call.args
        plan = context.execution_plan
        try:
            self._check_plan_scope(call, plan, surface.allowed_write_paths)
        except ValueError as exc:
            return self._rejected(call, "write_scope_denied", str(exc))
        if tool["risky"]:
            failure = self._approval_failure(name, args, surface, plan)
            if failure is not None:
                return self._rejected(call, failure.code, failure.detail, failure.recovery)
            if surface.mode != "auto" or name == "run_shell":
                try:
                    for logical, target in plan.paths:
                        source = args["path"] if name in {"write_file", "edit_file"} else logical
                        if agent.workspace.resolve_tool_path(source) != target:
                            raise ValueError("approved target changed; request approval again")
                except ValueError as exc:
                    return self._rejected(call, "approval_context_changed", str(exc), "retry_after_change")
        try:
            potential_paths = plan.paths
            effects_before = {} if name == "edit_file" else self._effect_snapshot(agent, potential_paths)
        except Exception as exc:  # noqa: BLE001 - effect planning boundary
            return self._rejected(call, "effect_planning_failed", str(exc), "retry_after_change")
        if name == "edit_file":
            return self._execute_edit(call, tool, context, plan)
        return self._execute_prepared(
            call,
            tool,
            context,
            plan,
            effects_before=effects_before,
        )

    def _rejected(
        self,
        call,
        code,
        detail,
        recovery="no_retry",
        *,
        structured=None,
    ):
        outcome = self._outcome(
            call,
            "rejected",
            "not_started",
            "none",
            "",
            failure=FailureInfo(code, detail, recovery),
            structured=structured,
        )
        self.runtime.run.run_log.append_tool_result(outcome)
        return outcome

    def _outcome(
        self,
        call,
        status,
        execution_state,
        side_effect_state,
        content,
        *,
        failure=None,
        affected_paths=(),
        structured=None,
        artifact_id="",
    ):
        return self.prepare_outcome(ToolOutcome(
            tool_call_id=call.call_id,
            tool_name=call.name,
            status=status,
            execution_state=execution_state,
            side_effect_state=side_effect_state,
            content=content,
            structured=dict(structured or {}),
            failure=failure,
            affected_paths=tuple(affected_paths),
            artifact_id=artifact_id,
        ))

    def prepare_outcome(self, outcome):
        """Prepare executed and recovered facts through the same output boundary."""
        failure = outcome.failure
        if failure is not None:
            failure = replace(failure, detail=self.runtime.redact_text(failure.detail))
        safe_content = self.runtime.redact_text(outcome.content)
        outcome = replace(
            outcome,
            content=safe_content,
            structured=redact_facts(outcome.structured, self.runtime.redact_text),
            failure=failure,
        )
        full_output = json.dumps(
            outcome.model_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if outcome.artifact_id or len(full_output.encode("utf-8")) <= TOOL_OUTPUT_MAX_BYTES:
            return outcome

        descriptor = self.runtime.dependencies.artifacts.write_tool_output(
            _run_id(self.runtime),
            outcome.tool_call_id,
            full_output,
        )
        bounded_failure = (
            FailureInfo(
                failure.code,
                clip(failure.detail, 1000),
                failure.recovery,
            )
            if failure is not None
            else None
        )
        return replace(
            outcome,
            content=(
                head_tail(safe_content)
                + "\n[Full tool result: artifact_id="
                + descriptor["artifact_id"]
                + ". Use read_artifact to inspect it.]"
            ),
            failure=bounded_failure,
            artifact_id=descriptor["artifact_id"],
        )
