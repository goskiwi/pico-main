"""The one public runtime boundary for model-visible tools."""

from __future__ import annotations

import json
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from io import BytesIO
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
    exclusions: dict[str, FailureInfo]

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
        self.read_versions: dict[str, str] = {}
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
                detail = (
                    "tool call was persisted but never entered execution; "
                    "reassess the current task, permissions and inputs before issuing a new call; "
                    "the old call will not be replayed automatically"
                )
                outcome = ToolOutcome(
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    status="error",
                    execution_state="not_started",
                    side_effect_state="none",
                    content="",
                    failure=FailureInfo(
                        "operation_not_started",
                        detail,
                        "retry_after_change",
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
                    content="",
                    failure=FailureInfo(
                        "operation_interrupted",
                        detail,
                        "no_retry" if uncertain else "retry_after_change",
                    ),
                    affected_paths=tuple(changed),
                    effect_scope=effect_scope if changed or unknown else "none",
                    structured={"path_transitions": transitions},
                )
            outcome = self.prepare_outcome(outcome)
            entry = run_log.append_tool_result(
                outcome,
                recovered_from_interruption=True,
            )
            reconciled.append((outcome, entry))
        return tuple(reconciled)

    def _build_registry(self):
        runtime = self.runtime
        tools = toolkit.build_tool_registry(
            workspace_root=runtime.workspace.root,
            path_resolver=runtime.workspace.resolve_tool_path,
            artifact_store=runtime.dependencies.artifacts,
            redact_text=runtime.redact_text,
            mutation_service=runtime.dependencies.mutations,
            command_runner=runtime.dependencies.command_runner,
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

    def _validate_args(self, name, args, tool, context):
        validated = tool["args_schema"].model_validate(args or {}).model_dump()
        context.execution_plan = self._plan_execution(tool, context, validated)
        validator = tool.get("validate")
        if validator is not None:
            validated = validator(context, validated)
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
        return not (mode == "auto" and name == "run_shell")

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
        )

    def context(self, *, call_id, execution_context=None):
        runtime = self.runtime
        return ToolContext(
            run_id=str(runtime.run.projection.run_id or "manual"),
            tool_call_id=str(call_id),
            execution_context=(
                execution_context
                if execution_context is not None
                else runtime.run.execution_context
            ),
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
            if before_state == "absent":
                artifacts[logical] = ""
                continue
            if not path.is_file():
                raise ValueError(f"workspace preimage is not a file: {logical}")
            with path.open("rb") as source:
                descriptor = agent.dependencies.artifacts.write_workspace_preimage(
                    _run_id(agent), call.call_id, logical, source,
                )
            captured = "sha256:" + descriptor["sha256"]
            if captured != before_state:
                raise RevisionConflict(logical, before_state, captured)
            artifacts[logical] = descriptor["artifact_id"]
        return artifacts

    def _validate_call(self, call, tool, context, *, record=True):
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
    def _invoke_runner(tool, context, args):
        execution = context.execution_context
        if execution is not None:
            execution.check_active()
        result = tool["run"](context, args)
        if not isinstance(result, ToolRunnerResult):
            raise TypeError("tool runner must return ToolRunnerResult")
        return result

    def _result_outcome(self, call, result, preimages=None):
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
                result.content,
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
                "",
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
            "",
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

    def execute_pending_group(self, group_id, surface):
        if not isinstance(surface, ResolvedToolSurface):
            raise TypeError("group execution requires its resolved Tool surface")
        calls = self._pending_group(group_id)
        return tuple(self._execute(call, surface) for call in calls)

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
        return self._execute(
            call, self.resolve_surface( manual=True),
        )

    def _check_plan_scope(self, call, plan, allowed_paths):
        self._require_write_scope((path for path, _target in plan.paths), allowed_paths)

    def _approval_failure(self, name, args, surface, plan):
        if surface.mode == "auto":
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
        expected_revision = self.read_versions.get(logical)
        if expected_revision is None:
            return self._rejected(
                call,
                "read_required",
                "read the file before editing it",
                "retry_after_change",
            )
        with ExitStack() as stack:
            try:
                raw, revision = stack.enter_context(agent.dependencies.mutations.prepare_edit(
                    path, expected_revision,
                    execution_context=context.execution_context,
                ))
                before = {logical: revision}
                drift = tracked_workspace_drift(before, plan.effect_scope, agent.run.evidence.change_set.files)
                if drift:
                    return self._rejected(
                        call, "workspace_drift", f"workspace changed outside this Run; read_file before continuing: {logical}",
                        "retry_after_change", structured={"drift": list(drift)},
                    )
                descriptor = agent.dependencies.artifacts.write_workspace_preimage(
                    _run_id(agent), call.call_id, logical, BytesIO(raw),
                )
                preimages = {logical: descriptor["artifact_id"]}
                context.execution_context.check_active()
            except ToolFailureError as exc:
                return self._rejected(call, exc.failure.code, exc.failure.detail,
                                      exc.failure.recovery, structured=exc.structured)
            except (ExecutionCancelled, ExecutionDeadlineExceeded):
                raise
            except Exception as exc:  # noqa: BLE001 - mutation planning boundary
                return self._rejected(call, "effect_planning_failed", str(exc), "retry_after_change")

            # Failure to persist intent must escape; never perform the edit or
            # append a speculative rejection over an ambiguous durable write.
            self._record_tool_started(agent, call, plan=plan, potential_effects=[{
                "path": logical, "before_state": revision,
                "before_artifact_id": descriptor["artifact_id"],
            }])
            context.execution_plan = plan
            bound = {
                **tool,
                "run": partial(
                    tool["run"],
                    original=raw,
                    expected_revision=expected_revision,
                ),
            }
            try:
                result = self._invoke_runner(bound, context, call.args)
            except Exception as exc:  # noqa: BLE001 - mutation runner boundary
                after = self._effect_snapshot(agent, plan.paths, settling=True)
                outcome = self._observed_exception_outcome(
                    call, exc, effects_before=before, effects_after=after,
                    preimages=preimages, potential_scope=plan.effect_scope,
                )
            else:
                after = self._effect_snapshot(agent, plan.paths, settling=True)
                outcome = self._observed_result_outcome(
                    call, result, effects_before=before, effects_after=after,
                    preimages=preimages, potential_scope=plan.effect_scope,
                )
        self._record_tool_result(agent, outcome)
        return outcome

    def _execute(self, call, surface):  # noqa: C901 - tool transaction boundary
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
        workspace_mutating = bool(tool.get("workspace_mutating", False))
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
            if surface.mode != "auto":
                try:
                    current = self.resolve_surface()
                    if name not in current.definitions:
                        raise ValueError("tool permission changed during approval")
                    self._check_plan_scope(call, plan, current.allowed_write_paths)
                    for logical, target in plan.paths:
                        source = args["path"] if name in {"write_file", "edit_file"} else logical
                        if agent.workspace.resolve_tool_path(source) != target:
                            raise ValueError("approved target changed; request approval again")
                except ValueError as exc:
                    return self._rejected(call, "approval_context_changed", str(exc), "retry_after_change")
        try:
            potential_scope, potential_paths = plan.effect_scope, plan.paths
            effects_before = {} if name == "edit_file" else self._effect_snapshot(agent, potential_paths)
        except Exception as exc:  # noqa: BLE001 - effect planning boundary
            return self._rejected(call, "effect_planning_failed", str(exc), "retry_after_change")
        if name == "edit_file":
            return self._execute_edit(call, tool, context, plan)
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
                f"workspace changed outside this Run; read_file before continuing: {paths}",
                "retry_after_change",
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
                )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            if potential_paths:
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
                    "",
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
            "",
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
        path = outcome.structured.get("path")
        if path and outcome.status == "success":
            revision = (
                outcome.structured.get("revision")
                if outcome.tool_name == "read_file"
                else outcome.structured.get("after_revision")
            )
            if revision and outcome.tool_name in {"read_file", "write_file", "edit_file"}:
                self.read_versions[path] = revision
        if outcome.tool_name == "read_file":
            observed = outcome.structured
            known = self.runtime.run.evidence.change_set.files.get(observed.get("path"))
            if (
                known is not None
                and observed.get("revision")
                and observed["revision"] != known.current_after_state
                and (
                    outcome.status == "success"
                    or (outcome.failure and outcome.failure.code == "missing_path")
                )
            ):
                outcome = replace(
                    outcome,
                    structured={**observed, "external_change_observed": True},
                )
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
