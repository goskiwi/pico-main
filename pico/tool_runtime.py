"""The one public runtime boundary for model-visible tools."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import tools as toolkit
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
from .mutations import RevisionConflict
from .security import redact_facts
from .tool_context import ToolContext
from .tool_execution import (
    attach_preimage_artifacts,
    classify_runner_result,
    effect_diff,
    intersect_write_scopes,
    path_transitions,
    tracked_workspace_drift,
)
from .workspace import clip

if TYPE_CHECKING:
    from .runtime import Pico

ASK_TOOL_NAMES = frozenset(
    {
        "list_files",
        "read_file",
        "read_artifact",
        "search",
        "update_working_state",
        "submit_final",
    }
)
EFFECT_SETTLEMENT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class PreparedParallelCall:
    call: ToolCall
    tool: dict
    context: ToolContext


@dataclass(slots=True)
class ResolvedToolSurface:
    """The one tool authority shared by a model turn and local execution."""

    mode: str
    allowed_write_paths: tuple[str, ...] | None
    definitions: dict[str, dict]
    action_tools: tuple[dict, ...]
    exclusions: dict[str, FailureInfo]
    tool_budget_exhausted: bool = False

    @property
    def names(self):
        return tuple(str(tool["name"]) for tool in self.action_tools)

    @property
    def policy(self):
        return self.mode, self.allowed_write_paths


def _run_id(agent):
    return str(agent.run.projection.run_id or "manual")


class ToolRuntime:
    """Own tool discovery, policy admission, and transaction execution."""

    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.registry = self._build_registry()
        self._apply_allowlist(self.registry)

    def reconcile_interrupted(self):
        """Close pending Tool transactions without replaying their Runners."""

        runtime = self.runtime
        run_log = runtime.run.run_log
        if run_log is None:
            return ()
        pending_calls = run_log.pending_tool_calls()
        if not pending_calls:
            return ()
        started_by_id = run_log.pending_tool_starts()
        reconciled = []
        observation_context = ExecutionContext.standalone(
            max_seconds=EFFECT_SETTLEMENT_TIMEOUT_SECONDS
        )
        for call in pending_calls:
            started = started_by_id.get(call.call_id)
            if started is None:
                detail = "tool call was persisted but never entered execution"
                outcome = ToolOutcome(
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    status="error",
                    execution_state="not_started",
                    side_effect_state="none",
                    content=detail,
                    failure=FailureInfo(
                        "operation_not_started",
                        detail,
                        "retry_after_wait",
                    ),
                )
            else:
                potential = list(started.payload.get("potential_effects", []))
                changed = []
                transitions = []
                for effect in potential:
                    logical = str(effect.get("path", ""))
                    if not logical:
                        continue
                    path = Path(logical)
                    if not path.is_absolute():
                        path = runtime.workspace.resolve_path(logical)
                    before = str(effect.get("before_state", ""))
                    before_artifact_id = str(effect.get("before_artifact_id", ""))
                    after = runtime.workspace.path_state(
                        path,
                        execution_context=observation_context,
                    )
                    if before != after:
                        changed.append(logical)
                        transitions.append(
                            {
                                "path": logical,
                                "before_state": before,
                                "after_state": after,
                                "before_artifact_id": before_artifact_id,
                            }
                        )
                effect_scope = str(started.payload.get("effect_scope", "none"))
                unknown = effect_scope == "workspace" and not potential
                uncertain = bool(changed or unknown)
                detail = "tool execution was interrupted before a durable result"
                outcome = ToolOutcome(
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    status="partial_success" if uncertain else "error",
                    execution_state="failed",
                    side_effect_state=(
                        "partial"
                        if changed
                        else ("unknown" if unknown else "none")
                    ),
                    content=detail,
                    failure=FailureInfo(
                        "operation_interrupted",
                        detail,
                        "no_retry" if uncertain else "retry_after_wait",
                    ),
                    affected_paths=tuple(changed),
                    effect_scope=effect_scope if changed or unknown else "none",
                    structured={"path_transitions": transitions},
                )
            if started is not None:
                if call.name == "delegate":
                    from .subagents.runner import recover_delegate

                    outcome = recover_delegate(runtime, call)
                elif call.name == "integrate_child":
                    from .subagents.integration import PatchIntegrator

                    outcome = PatchIntegrator(runtime).recover_applied(
                        call,
                        started,
                        outcome,
                    )
            outcome = self.prepare_outcome(outcome)
            entry = run_log.append_tool_result(
                outcome,
                recovered_from_interruption=True,
            )
            reconciled.append((outcome, entry))
        return tuple(reconciled)

    def _build_registry(self):
        tools = toolkit.build_tool_registry()
        from .checks import build_tool_registry as build_check_registry
        from .subagents.tools import build_tool_registry as build_subagent_registry

        tools.update(
            build_check_registry(
                available=self.runtime.dependencies.check_runner is not None
            )
        )
        tools.update(
            build_subagent_registry(
                available=self.runtime.dependencies.subagents is not None
            )
        )
        return tools

    def _apply_allowlist(self, tools):
        allowed_tools = self.runtime.config.allowed_tools
        if allowed_tools is None:
            return tools
        unknown = [name for name in allowed_tools if name not in tools]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        unavailable = [
            name
            for name in allowed_tools
            if not tools[name].get("available", True)
        ]
        if unavailable:
            raise ValueError(
                "configured tool executor is unavailable: "
                + ", ".join(unavailable)
            )
        allowed = set(allowed_tools)
        return {name: tool for name, tool in tools.items() if name in allowed}

    def _validate_args(self, name, args, tool, context, policy):
        runtime = self.runtime
        validated = tool["args_schema"].model_validate(args or {}).model_dump()
        validator = tool.get("validate")
        if validator is not None:
            validated = validator(context, validated)
        allowed_paths = policy[1]
        if name in {"write_file", "edit_file"} and allowed_paths is not None:
            target = runtime.workspace.resolve_tool_path(validated["path"])
            relative = target.relative_to(runtime.workspace.root).as_posix()
            self._require_write_scope((relative,), allowed_paths)
        if (
            name == "delegate"
            and validated.get("role") == "implement"
            and allowed_paths is not None
        ):
            self._require_write_scope(validated["allowed_write_paths"], allowed_paths)
        if name == "integrate_child" and allowed_paths is not None:
            record = runtime.run.projection.children.record(
                validated["child_id"]
            )
            patch = record.completed().patch
            self._require_write_scope(
                (() if patch is None else patch.changed_paths), allowed_paths
            )
        return validated

    def _effective_policy(self):
        contract = self.runtime.run.projection.contract
        mode = self.runtime.config.mode
        if mode == "ask" or (
            contract is not None and contract.write_scope.mode == "none"
        ):
            mode = "ask"
        paths = intersect_write_scopes(
            contract.write_scope.allowed_paths() if contract is not None else None,
            self.runtime.config.allowed_write_paths,
        )
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
        return not (mode == "auto" and name == "run_command")

    def resolve_surface(self, *, manual=False):
        """Resolve one authoritative Tool set for advertisement and execution."""

        mode, allowed_write_paths = self._effective_policy()
        definitions = {}
        exclusions = {}
        configured = self.runtime.config.allowed_tools
        for name, tool in self.registry.items():
            if configured is not None and name not in configured:
                exclusions[name] = FailureInfo(
                    "tool_not_allowed",
                    "tool outside run surface",
                    "no_retry",
                )
                continue
            if not tool.get("available", True):
                exclusions[name] = FailureInfo(
                    "tool_unavailable",
                    f"{name} executor is unavailable",
                    "user_action_required",
                )
                continue
            if manual:
                if not tool.get("manual_observation", False):
                    exclusions[name] = FailureInfo(
                        "manual_mutation_forbidden",
                        "manual mode permits observation tools only; mutations "
                        "require an active Run",
                        "no_retry",
                    )
                    continue
            elif not self._tool_allowed_by_mode(name, mode):
                exclusions[name] = FailureInfo(
                    "tool_not_allowed",
                    f"tool is unavailable in {mode} mode",
                    "no_retry",
                )
                continue
            # Freeze this turn's definition membership and callbacks without
            # coupling execution to later Registry dictionary mutations.
            definitions[name] = dict(tool)

        budget_exhausted = not manual and self.remaining_budget() == 0
        if budget_exhausted:
            for name in definitions:
                exclusions[name] = FailureInfo(
                    "tool_execution_limit",
                    "Runtime tool budget exhausted",
                    "no_retry",
                )
            definitions = {}
        action_tools = (
            ()
            if manual
            else tuple(toolkit.build_action_tools(definitions))
        )
        return ResolvedToolSurface(
            mode=mode,
            allowed_write_paths=allowed_write_paths,
            definitions=definitions,
            action_tools=action_tools,
            exclusions=exclusions,
            tool_budget_exhausted=budget_exhausted,
        )

    def remaining_budget(self):
        limit = self.runtime.config.max_tool_executions
        if limit is None:
            return None
        executed = self.runtime.run.metrics.executed_tool_count - self.runtime.run.request_tool_start
        return max(0, limit - executed)

    def context(self, *, call_id, execution_context=None):
        runtime = self.runtime
        return ToolContext(
            workspace_root=runtime.workspace.root,
            path_resolver=runtime.workspace.resolve_tool_path,
            artifact_store=runtime.dependencies.artifacts,
            redact_text=runtime.redact_text,
            run_id=str(runtime.run.projection.run_id or "manual"),
            tool_call_id=str(call_id),
            working_state=(
                runtime.run.projection.working
                if runtime.run.projection.contract is not None
                else None
            ),
            execution_context=(
                execution_context
                if execution_context is not None
                else runtime.run.execution_context
            ),
            mutation_service=runtime.dependencies.mutations,
            command_runner=runtime.dependencies.command_runner,
            check_runner=runtime.dependencies.check_runner,
            subagent_service=runtime.dependencies.subagents,
        )

    def _resolve_tool(self, call, surface, *, record=True):
        tool = surface.definitions.get(call.name)
        if tool is not None:
            return tool, None
        failure = surface.exclusions.get(call.name)
        if failure is not None:
            return None, self._rejected(
                call,
                failure.code,
                failure.detail,
                failure.recovery,
                record=record,
            )
        if call.name not in self.registry:
            return None, self._rejected(
                call, "unknown_tool", "unknown tool", "retry_after_change",
                record=record,
            )
        raise RuntimeError(f"resolved Tool surface is incomplete for {call.name}")

    @staticmethod
    def _recorded_run_log(agent, call_id):
        run_log = agent.run.run_log
        if run_log is None:
            return None
        pending = run_log.pending_tool_calls()
        if not pending:
            raise RuntimeError("active Run has no pending tool call")
        if str(call_id) not in {call.call_id for call in pending}:
            raise RuntimeError("tool execution does not match the pending Run call")
        return run_log

    @classmethod
    def _record_tool_started(cls, agent, call, *, plan, potential_effects):
        run_log = cls._recorded_run_log(agent, call.call_id)
        if run_log is None:
            return None
        return run_log.append_tool_started(
            call,
            effect_scope=plan.effect_scope,
            potential_effects=potential_effects,
            operation=plan.operation,
        )

    @classmethod
    def _record_tool_result(cls, agent, outcome):
        run_log = cls._recorded_run_log(agent, outcome.tool_call_id)
        if run_log is None:
            return None
        return run_log.append_tool_result(outcome)

    @staticmethod
    def _plan_execution(tool, context, args):
        planner = tool.get("plan")
        if planner is not None:
            plan = planner(context, args)
            if not isinstance(plan, ToolExecutionPlan):
                raise TypeError("tool planner must return ToolExecutionPlan")
            return plan
        return ToolExecutionPlan(
            "workspace" if tool.get("workspace_mutating", False) else "none"
        )

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

    @staticmethod
    def _preimage_artifacts(agent, call, paths, states, effect_scope):
        if (
            effect_scope != "workspace"
            or agent.run.run_log is None
            or agent.run.projection.contract is None
        ):
            return {}
        artifacts = {}
        for logical, path in paths:
            before_state = states.get(logical, "absent")
            if call.name == "edit_file" and call.args["expected_revision"] != before_state:
                raise RevisionConflict(logical, call.args["expected_revision"], before_state)
            if before_state == "absent":
                artifacts[logical] = ""
                continue
            if not path.is_file():
                raise ValueError(f"workspace preimage is not a file: {logical}")
            descriptor = agent.dependencies.artifacts.write_workspace_preimage(
                _run_id(agent),
                call.call_id,
                logical,
                path,
            )
            captured = "sha256:" + descriptor["sha256"]
            if captured != before_state:
                raise RevisionConflict(logical, before_state, captured)
            artifacts[logical] = descriptor["artifact_id"]
        return artifacts

    def _validate_call(self, call, tool, context, policy, *, record=True):
        try:
            args = self._validate_args(
                call.name, call.args, tool, context, policy
            )
        except ToolFailureError as exc:
            return None, self._rejected(
                call,
                exc.failure.code,
                exc.failure.detail,
                exc.failure.recovery,
                structured=exc.structured, record=record,
            )
        except Exception as exc:  # noqa: BLE001 - validator boundary
            return None, self._rejected(
                call,
                "invalid_arguments",
                str(exc),
                "retry_after_change", record=record,
            )
        return ToolCall(call.name, args, call.call_id), None

    def _pending_group(self, group_id):
        runtime = self.runtime
        if runtime.run.resumable:
            raise RuntimeError("a dormant Run must be resumed before grouped execution")
        if runtime.run.projection.contract is None or runtime.run.run_log is None:
            raise RuntimeError("pending grouped execution requires an active Run")
        if runtime.run.projection.terminal:
            raise RuntimeError("terminal Run cannot execute a tool group")
        if runtime.run.run_log.pending_group_id() != str(group_id):
            raise RuntimeError("group id does not match the pending tool calls")
        calls = runtime.run.run_log.pending_tool_calls()
        if not calls:
            raise RuntimeError("active Run has no pending tool calls")
        return calls

    @staticmethod
    def _parallel_safe(tool):
        return bool(
            tool.get("concurrency") == "parallel"
            and not tool.get("risky", False)
            and not tool.get("workspace_mutating", False)
            and not tool.get("state_mutating", False)
        )

    def _prepare_parallel_call(self, call, surface):
        tool, rejection = self._resolve_tool(call, surface, record=False)
        if rejection is not None:
            return rejection
        if not self._parallel_safe(tool):
            raise RuntimeError(f"tool is not parallel-safe: {call.name}")
        execution = (
            self.runtime.run.execution_context.child()
            if self.runtime.run.execution_context is not None
            else None
        )
        context = self.context(call_id=call.call_id, execution_context=execution)
        call, rejection = self._validate_call(
            call, tool, context, surface.policy, record=False
        )
        if rejection is not None:
            return rejection
        return PreparedParallelCall(call, tool, context)

    @staticmethod
    def _invoke_runner(tool, context, args):
        execution = context.execution_context
        if execution is not None:
            execution.check_active()
        result = tool["run"](context, args)
        if not isinstance(result, ToolRunnerResult):
            raise TypeError("tool runner must return ToolRunnerResult")
        if execution is not None and tool.get("concurrency") == "parallel":
            execution.check_active()
        return result

    def _parallel_outcome(self, prepared, result):
        call = prepared.call
        if isinstance(result, BaseException):
            typed = result if isinstance(result, ToolFailureError) else None
            interrupted = isinstance(
                result,
                (ExecutionCancelled, ExecutionDeadlineExceeded),
            )
            return self._outcome(
                call,
                "error",
                "failed",
                "none",
                f"error: parallel tool {call.name} failed: {result}",
                failure=(typed.failure if typed else None)
                or FailureInfo(
                    "operation_interrupted" if interrupted else "observation_failed",
                    str(result),
                    "retry_after_wait" if interrupted else "retry_after_change",
                ),
                structured=typed.structured if typed else None,
            )
        return self._result_outcome(call, result, parallel=True)

    def _result_outcome(self, call, result, preimages=None, *, parallel=False):
        if parallel and (result.effect_scope != "none" or result.affected_paths):
            paths = tuple(result.affected_paths)
            return self._outcome(
                call,
                "partial_success",
                "completed",
                "unknown",
                "error: parallel-safe tool reported a side effect\n"
                + str(result.content),
                failure=FailureInfo(
                    "parallel_tool_reported_side_effect",
                    "parallel-safe tool reported a side effect",
                    "user_action_required",
                ),
                affected_paths=paths,
                effect_scope=result.effect_scope,
                structured=attach_preimage_artifacts(
                    result.structured, preimages or {}
                ),
            )
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
            effect_scope=result.effect_scope,
            structured=attach_preimage_artifacts(result.structured, preimages or {}),
        )

    @staticmethod
    def _observed_structured(structured, transitions):
        observed = dict(structured or {})
        observed.pop("path_transitions", None)
        if transitions:
            observed["path_transitions"] = list(transitions)
        return observed

    def _observed_result_outcome(
        self,
        call,
        result,
        *,
        effects_before,
        effects_after,
        preimages,
        potential_scope,
    ):
        planned_paths = set(effects_before)
        unexpected = sorted(set(result.affected_paths) - planned_paths)
        if unexpected:
            return self._outcome(
                call,
                "partial_success",
                "completed",
                "unknown",
                "error: tool reported effects outside its resolved plan\n"
                + str(result.content),
                failure=FailureInfo(
                    "tool_effect_outside_plan",
                    "tool reported unplanned paths: " + ", ".join(unexpected),
                    "user_action_required",
                ),
                affected_paths=unexpected,
                effect_scope=potential_scope,
                structured={
                    **self._observed_structured(result.structured, ()),
                    "reported_unplanned_paths": unexpected,
                },
            )
        paths = effect_diff(effects_before, effects_after)
        transitions = path_transitions(
            effects_before,
            effects_after,
            preimages,
            paths,
        )
        effect_scope = potential_scope if paths else "none"
        status, side_effect, paths = classify_runner_result(
            result.failure,
            paths,
            effect_scope,
        )
        return self._outcome(
            call,
            status,
            "completed",
            side_effect,
            result.content,
            failure=result.failure,
            affected_paths=paths,
            effect_scope=effect_scope,
            structured=self._observed_structured(
                result.structured,
                transitions,
            ),
        )

    def _observed_exception_outcome(
        self,
        call,
        error,
        *,
        effects_before,
        effects_after,
        preimages,
        potential_scope,
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
                f"error: tool {call.name} failed: {error}",
                failure=typed.failure,
                structured=self._observed_structured(typed.structured, ()),
            )
        paths = effect_diff(effects_before, effects_after)
        transitions = path_transitions(
            effects_before,
            effects_after,
            preimages,
            paths,
        )
        uncertain = bool(paths)
        return self._outcome(
            call,
            "partial_success" if uncertain else "error",
            "failed",
            "partial" if uncertain else "none",
            f"error: tool {call.name} failed: {error}",
            failure=FailureInfo(
                "tool_partial_success" if uncertain else "tool_failed",
                str(error),
                "no_retry" if uncertain else "retry_after_change",
            ),
            affected_paths=paths,
            effect_scope=potential_scope if paths else "none",
            structured=self._observed_structured(
                {},
                transitions,
            ),
        )

    def _execute_parallel_segment(self, calls, surface):
        prepared = [self._prepare_parallel_call(call, surface) for call in calls]
        run_log = self.runtime.run.run_log
        outcomes = []
        index = 0
        while index < len(prepared):
            item = prepared[index]
            if isinstance(item, ToolOutcome):
                run_log.append_tool_result(item)
                outcomes.append(item)
                index += 1
                continue
            remaining = self.remaining_budget()
            if remaining == 0:
                outcome = self._rejected(
                    item.call, "tool_execution_limit",
                    "Runtime tool budget exhausted", record=True,
                )
                outcomes.append(outcome)
                index += 1
                continue
            group = []
            while index < len(prepared) and isinstance(
                prepared[index], PreparedParallelCall
            ):
                if remaining is not None and len(group) >= remaining:
                    break
                group.append(prepared[index])
                index += 1
            for candidate in group:
                run_log.append_tool_started(
                    candidate.call,
                    effect_scope="none",
                    potential_effects=[],
                    operation={},
                )
            with ThreadPoolExecutor(
                max_workers=min(self.runtime.config.max_parallel_tools, len(group)),
                thread_name_prefix="pico-tool",
            ) as pool:
                futures = [
                    pool.submit(
                        self._invoke_runner,
                        candidate.tool,
                        candidate.context,
                        candidate.call.args,
                    )
                    for candidate in group
                ]
                raw_results = []
                for future in futures:
                    try:
                        raw_results.append(future.result())
                    except Exception as exc:  # noqa: BLE001 - tool runner boundary
                        raw_results.append(exc)
            for candidate, raw in zip(group, raw_results):
                outcome = self._parallel_outcome(candidate, raw)
                run_log.append_tool_result(outcome)
                outcomes.append(outcome)
        return tuple(outcomes)

    def execute_pending_group(self, group_id, surface):
        if not isinstance(surface, ResolvedToolSurface):
            raise TypeError("group execution requires its resolved Tool surface")
        calls = self._pending_group(group_id)
        outcomes = []
        parallel = []

        def flush():
            if parallel:
                outcomes.extend(
                    self._execute_parallel_segment(tuple(parallel), surface)
                )
                parallel.clear()

        for call in calls:
            tool = surface.definitions.get(call.name)
            if tool is not None and self._parallel_safe(tool):
                parallel.append(call)
                continue
            flush()
            outcomes.append(self._execute(call, surface))
        flush()
        return tuple(outcomes)

    def execute_manual(self, name, args=None):
        call = ToolCall(str(name), dict(args or {}))
        agent = self.runtime
        if agent.run.resumable:
            return self._rejected(
                call,
                "run_protocol_violation",
                "a dormant Run must be resumed before manual tools",
                "no_retry",
                record=False,
            )
        if agent.run.projection.contract is not None:
            return self._rejected(
                call,
                "run_protocol_violation",
                "manual tools require no active or terminal Run",
                "no_retry",
                record=False,
            )
        return self._execute(call, self.resolve_surface(manual=True))

    def _approval_failure(self, name, args, surface):
        if surface.mode == "auto":
            return None
        handler = self.runtime.dependencies.approval_handler
        if handler is None:
            return FailureInfo("approval_denied", "approval denied", "no_retry")
        try:
            approved = bool(handler(name, dict(args)))
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
        if self.remaining_budget() == 0:
            return self._rejected(
                call, "tool_execution_limit", "Runtime tool budget exhausted"
            )
        tool, admission_rejection = self._resolve_tool(call, surface)
        if admission_rejection is not None:
            return admission_rejection
        workspace_mutating = bool(tool.get("workspace_mutating", False))
        context = self.context(call_id=call.call_id)
        call, validation_rejection = self._validate_call(
            call, tool, context, surface.policy
        )
        if validation_rejection is not None:
            return validation_rejection
        args = call.args
        if tool["risky"]:
            failure = self._approval_failure(name, args, surface)
            if failure is not None:
                return self._rejected(
                    call,
                    failure.code,
                    failure.detail,
                    failure.recovery,
                )

        try:
            plan = self._plan_execution(tool, context, args)
            potential_scope, potential_paths = plan.effect_scope, plan.paths
            self._require_write_scope(
                (logical for logical, _path in potential_paths),
                surface.allowed_write_paths,
            )
            effects_before = self._effect_snapshot(agent, potential_paths)
        except Exception as exc:  # noqa: BLE001 - fail before side effect
            return self._rejected(
                call, "effect_planning_failed", str(exc), "retry_after_change"
            )
        drift = tracked_workspace_drift(
            effects_before,
            potential_scope,
            agent.run.evidence.change_set.files,
        )
        if drift:
            paths = ", ".join(item["path"] for item in drift)
            return self._rejected(
                call,
                "workspace_drift",
                f"workspace changed outside this Run after its last mutation: {paths}",
                "user_action_required",
                structured={"drift": list(drift)},
            )
        try:
            preimages = self._preimage_artifacts(
                agent, call, potential_paths, effects_before, potential_scope
            )
        except ToolFailureError as exc:
            return self._rejected(call, exc.failure.code, exc.failure.detail,
                                  exc.failure.recovery, structured=exc.structured)
        except Exception as exc:  # noqa: BLE001 - fail before side effect
            return self._rejected(
                call, "effect_planning_failed", str(exc), "retry_after_change"
            )
        self._record_tool_started(
            agent,
            call,
            plan=plan,
            potential_effects=[
                {
                    "path": path,
                    "before_state": state,
                    "before_artifact_id": preimages.get(path, ""),
                }
                for path, state in sorted(effects_before.items())
            ],
        )
        context.execution_plan = plan

        try:
            execution = self._invoke_runner(tool, context, args)
            if potential_paths:
                effects_after = self._effect_snapshot(
                    agent,
                    potential_paths,
                    settling=True,
                )
                outcome = self._observed_result_outcome(
                    call,
                    execution,
                    effects_before=effects_before,
                    effects_after=effects_after,
                    preimages=preimages,
                    potential_scope=potential_scope,
                )
            else:
                outcome = self._result_outcome(
                    call,
                    execution,
                    preimages,
                    parallel=self._parallel_safe(tool),
                )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            if self._parallel_safe(tool):
                outcome = self._parallel_outcome(
                    PreparedParallelCall(call, tool, context), exc
                )
            elif potential_paths:
                effects_after = self._effect_snapshot(
                    agent,
                    potential_paths,
                    settling=True,
                )
                outcome = self._observed_exception_outcome(
                    call,
                    exc,
                    effects_before=effects_before,
                    effects_after=effects_after,
                    preimages=preimages,
                    potential_scope=potential_scope,
                )
            else:
                effects_after = self._effect_snapshot(
                    agent,
                    potential_paths,
                    settling=True,
                )
                detected_paths = effect_diff(effects_before, effects_after)
                typed_error = exc if isinstance(exc, ToolFailureError) else None
                paths = [] if typed_error else detected_paths
                unknown = bool(
                    not typed_error and workspace_mutating and not potential_paths
                )
                uncertain = bool(paths or unknown)
                transitions = path_transitions(
                    effects_before,
                    effects_after,
                    preimages,
                    paths,
                )
                outcome = self._outcome(
                    call,
                    "partial_success" if uncertain else "error",
                    "failed",
                    "partial" if paths else ("unknown" if unknown else "none"),
                    f"error: tool {name} failed: {exc}",
                    failure=(typed_error.failure if typed_error else None)
                    or FailureInfo(
                        "tool_partial_success"
                        if paths
                        else ("tool_effect_unknown" if unknown else "tool_failed"),
                        str(exc),
                        "no_retry" if uncertain else "retry_after_change",
                    ),
                    affected_paths=paths,
                    effect_scope=potential_scope,
                    structured=(
                        typed_error.structured
                        if typed_error
                        else {"path_transitions": transitions}
                    ),
                )

        self._record_tool_result(agent, outcome)
        return outcome

    def _rejected(
        self,
        call,
        code,
        detail,
        recovery="no_retry",
        *,
        structured=None,
        record=True,
    ):
        outcome = self._outcome(
            call,
            "rejected",
            "not_started",
            "none",
            f"error: {detail} for {call.name}",
            failure=FailureInfo(code, detail, recovery),
            structured=structured,
        )
        if record:
            self._record_tool_result(self.runtime, outcome)
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
        effect_scope="none",
        structured=None,
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
            effect_scope=effect_scope if side_effect_state != "none" else "none",
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
        if len(full_output.encode("utf-8")) <= TOOL_OUTPUT_MAX_BYTES:
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
                clip(safe_content, 2000)
                + "\n[Full tool result: artifact_id="
                + descriptor["artifact_id"]
                + ". Use read_artifact to inspect it.]"
            ),
            failure=bounded_failure,
            artifact_id=descriptor["artifact_id"],
        )
