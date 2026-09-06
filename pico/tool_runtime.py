"""The one public runtime boundary for model-visible tools."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import tools as toolkit
from .contracts import (
    FailureInfo,
    ToolCall,
    ToolFailureError,
    ToolOutcome,
    ToolRunnerResult,
)
from .execution import ExecutionCancelled, ExecutionDeadlineExceeded
from .mutations import RevisionConflict
from .security import redact_facts
from .tool_context import ToolContext
from .tool_execution import (
    DEFAULT_TOOL_PREVIEW_BYTES,
    attach_preimage_artifacts,
    classify_runner_result,
    effect_diff,
    intersect_write_scopes,
    model_tool_output,
    path_transitions,
    tracked_workspace_drift,
)

if TYPE_CHECKING:
    from .runtime import Pico

MAX_OBSERVATION_BATCH_CALLS = 4
MAX_PARALLEL_OBSERVATIONS = 4
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


@dataclass(frozen=True)
class PreparedObservation:
    call: ToolCall
    tool: dict
    context: ToolContext


def _run_id(agent):
    return str(agent.run.projection.run_id or "manual")


class ToolRuntime:
    """Own tool discovery, policy admission, and transaction execution."""

    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.registry = self._build_registry()
        self._apply_allowlist(self.registry)

    def _surface(self, policy):
        return {
            name: tool
            for name, tool in self._apply_allowlist(self.registry).items()
            if self._tool_allowed_by_mode(name, policy[0])
        }

    def _build_registry(self):
        tools = toolkit.build_tool_registry()
        if self.runtime.dependencies.check_runner is not None:
            from .checks import build_tool_registry

            tools.update(build_tool_registry())
        if self.runtime.dependencies.subagents is not None:
            from .subagents.tools import build_tool_registry

            tools.update(build_tool_registry(self.runtime.dependencies.subagents))
        return tools

    def _apply_allowlist(self, tools):
        allowed_tools = self.runtime.config.allowed_tools
        if allowed_tools is None:
            return tools
        unknown = [name for name in allowed_tools if name not in tools]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(allowed_tools)
        return {name: tool for name, tool in tools.items() if name in allowed}

    def validate(self, name, args, context, policy):
        runtime = self.runtime
        tool = self.registry.get(name)
        if tool is None:
            raise ValueError(f"unknown tool: {name}")
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
            _scope, planned_paths = self._potential_effects(tool, context, validated)
            self._require_write_scope(
                (logical for logical, _path in planned_paths),
                allowed_paths,
            )
        return validated

    def effective_policy(self):
        contract = self.runtime.run.projection.contract
        mode = self.runtime.config.mode
        if mode == "ask" or (
            contract is not None and not contract.allows_workspace_mutation
        ):
            mode = "ask"
        paths = intersect_write_scopes(
            getattr(contract, "allowed_write_paths", None),
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

    def model_action_tools(self):
        return toolkit.build_action_tools(self._surface(self.effective_policy()))

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
        )

    def approve(self, name, args):
        if self.runtime.config.mode == "auto" and name != "run_command":
            return True
        try:
            answer = input(
                f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] "
            )
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def _pending_call(self, call_id):
        runtime = self.runtime
        if runtime.run.resumable:
            raise RuntimeError("a dormant Run must be resumed before tool execution")
        if runtime.run.projection.contract is None or runtime.run.run_log is None:
            raise RuntimeError("pending tool execution requires an active Run")
        if runtime.run.projection.terminal:
            raise RuntimeError("terminal Run cannot execute additional tools")
        call = runtime.run.run_log.pending_tool_call()
        if call is None:
            raise RuntimeError("active Run has no pending ToolCall fact")
        if call.call_id != str(call_id):
            raise RuntimeError("call id does not match the pending ToolCall")
        return call

    def _resolve_tool(self, call, policy):
        tool = self.registry.get(call.name)
        if tool is None:
            return None, self._rejected(
                call, "unknown_tool", "unknown tool", "retry_after_change"
            )
        if not self._tool_allowed_by_mode(call.name, policy[0]):
            return None, self._rejected(
                call,
                "tool_not_allowed",
                f"tool is unavailable in {policy[0]} mode",
                "no_retry",
            )
        allowed = self.runtime.config.allowed_tools
        if allowed is not None and call.name not in allowed:
            return None, self._rejected(
                call, "tool_not_allowed", "tool outside run surface"
            )
        if self.runtime.run.projection.contract is None and not tool.get(
            "manual_observation", False
        ):
            return None, self._rejected(
                call,
                "manual_mutation_forbidden",
                "manual mode permits observation tools only; mutations require an active Run",
                "no_retry",
            )
        return tool, None

    @staticmethod
    def _recorded_run_log(agent, call_id):
        run_log = agent.run.run_log
        if run_log is None:
            return None
        pending = run_log.pending_call_id()
        if not pending:
            raise RuntimeError("active Run has no pending tool call")
        if pending != str(call_id):
            raise RuntimeError("tool execution does not match the pending Run call")
        return run_log

    @classmethod
    def _record_tool_started(cls, agent, call, *, effect_scope, potential_effects):
        run_log = cls._recorded_run_log(agent, call.call_id)
        if run_log is None:
            return None
        return run_log.append_tool_started(
            call,
            effect_scope=effect_scope,
            potential_effects=potential_effects,
        )

    @classmethod
    def _record_tool_result(cls, agent, outcome):
        run_log = cls._recorded_run_log(agent, outcome.tool_call_id)
        if run_log is None:
            return None
        return run_log.append_tool_result(outcome)

    @staticmethod
    def _potential_effects(tool, context, args):
        planner = tool.get("potential_effects")
        if planner is not None:
            return planner(context, args)
        return ("workspace" if tool.get("workspace_mutating", False) else "none"), ()

    @staticmethod
    def _effect_snapshot(agent, paths):
        return {logical: agent.workspace.path_state(path) for logical, path in paths}

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

    def _validate_call(self, call, context, policy):
        try:
            args = self.validate(call.name, call.args, context, policy)
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

    def execute_pending(self, call_id):
        return self._execute(self._pending_call(call_id))

    def _pending_batch(self, batch_id):
        runtime = self.runtime
        if runtime.run.resumable:
            raise RuntimeError("a dormant Run must be resumed before batch execution")
        if runtime.run.projection.contract is None or runtime.run.run_log is None:
            raise RuntimeError("pending batch execution requires an active Run")
        if runtime.run.projection.terminal:
            raise RuntimeError("terminal Run cannot execute an observation batch")
        if runtime.run.run_log.pending_batch_id() != str(batch_id):
            raise RuntimeError("batch id does not match the pending Tool Batch")
        calls = runtime.run.run_log.pending_tool_calls()
        if len(calls) < 2:
            raise RuntimeError("active Run has no pending observation batch")
        return calls

    def _batch_policy_error(self, calls, policy):
        if len(calls) > MAX_OBSERVATION_BATCH_CALLS:
            return (
                f"observation batch contains {len(calls)} calls; "
                f"maximum is {MAX_OBSERVATION_BATCH_CALLS}"
            )
        remaining = self.remaining_budget()
        if remaining is not None and len(calls) > remaining:
            return "observation batch exceeds the remaining Runtime tool budget"
        invalid = []
        surface = self._surface(policy)
        for call in calls:
            tool = surface.get(call.name)
            if (
                tool is None
                or not tool.get("batchable_observation", False)
                or tool.get("risky", False)
                or tool.get("workspace_mutating", False)
                or tool.get("state_mutating", False)
            ):
                invalid.append(call.name or "<missing>")
        if invalid:
            return (
                "multiple calls are allowed only for independent observations; "
                "call these tools alone: " + ", ".join(invalid)
            )
        return ""

    def _prepare_observation_batch(self, calls):
        policy = self.effective_policy()
        detail = self._batch_policy_error(calls, policy)
        if detail:
            return (), detail
        prepared = []
        for call in calls:
            execution = (
                self.runtime.run.execution_context.child()
                if self.runtime.run.execution_context is not None
                else None
            )
            context = self.context(call_id=call.call_id, execution_context=execution)
            tool = self.registry[call.name]
            try:
                args = self.validate(call.name, call.args, context, policy)
            except Exception as exc:  # noqa: BLE001 - batch admission boundary
                return (), f"invalid arguments for {call.name}: {exc}"
            prepared.append(
                PreparedObservation(
                    ToolCall(call.name, args, call.call_id),
                    tool,
                    context,
                )
            )
        return tuple(prepared), ""

    def _reject_observation_batch(self, calls, detail):
        outcomes = []
        for call in calls:
            outcome = self._outcome(
                call,
                "rejected",
                "not_started",
                "none",
                f"error: {detail} for observation batch",
                failure=FailureInfo("invalid_tool_batch", detail, "retry_after_change"),
            )
            self.runtime.run.run_log.append_tool_result(outcome)
            outcomes.append(outcome)
        return tuple(outcomes)

    @staticmethod
    def _invoke_runner(tool, context, args):
        execution = context.execution_context
        if execution is not None:
            execution.check_active()
        result = tool["run"](context, args)
        if not isinstance(result, ToolRunnerResult):
            raise TypeError("tool runner must return ToolRunnerResult")
        if execution is not None and tool.get("batchable_observation", False):
            execution.check_active()
        return result

    def _observation_outcome(self, prepared, result):
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
                f"error: observation {call.name} failed: {result}",
                failure=(typed.failure if typed else None)
                or FailureInfo(
                    "operation_interrupted" if interrupted else "observation_failed",
                    str(result),
                    "retry_after_wait" if interrupted else "retry_after_change",
                ),
                structured=typed.structured if typed else None,
            )
        return self._result_outcome(call, result, observation=True)

    def _result_outcome(self, call, result, preimages=None, *, observation=False):
        if observation and (result.effect_scope != "none" or result.affected_paths):
            paths = tuple(result.affected_paths)
            return self._outcome(
                call,
                "partial_success",
                "completed",
                "unknown",
                "error: batchable observation reported a side effect\n"
                + str(result.content),
                failure=FailureInfo(
                    "observation_reported_side_effect",
                    "batchable observation reported a side effect",
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

    def execute_pending_batch(self, batch_id):
        calls = self._pending_batch(batch_id)
        prepared, detail = self._prepare_observation_batch(calls)
        if detail:
            return self._reject_observation_batch(calls, detail)
        run_log = self.runtime.run.run_log
        for item in prepared:
            run_log.append_tool_started(
                item.call,
                effect_scope="none",
                potential_effects=[],
            )
        with ThreadPoolExecutor(
            max_workers=min(MAX_PARALLEL_OBSERVATIONS, len(prepared)),
            thread_name_prefix="pico-observation",
        ) as pool:
            futures = [
                pool.submit(
                    self._invoke_runner, item.tool, item.context, item.call.args
                )
                for item in prepared
            ]
            raw_results = []
            for future in futures:
                try:
                    raw_results.append(future.result())
                except Exception as exc:  # noqa: BLE001 - tool runner boundary
                    raw_results.append(exc)
        outcomes = []
        for item, raw in zip(prepared, raw_results):
            outcome = self._observation_outcome(item, raw)
            run_log.append_tool_result(outcome)
            outcomes.append(outcome)
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
        return self._execute(call)

    def _execute(self, call):
        agent = self.runtime
        name, args = call.name, call.args
        policy = self.effective_policy()
        tool, admission_rejection = self._resolve_tool(call, policy)
        if admission_rejection is not None:
            return admission_rejection
        workspace_mutating = bool(tool.get("workspace_mutating", False))
        context = self.context(call_id=call.call_id)
        call, validation_rejection = self._validate_call(call, context, policy)
        if validation_rejection is not None:
            return validation_rejection
        args = call.args
        if tool["risky"] and not self.approve(name, args):
            return self._rejected(call, "approval_denied", "approval denied")

        try:
            potential_scope, potential_paths = self._potential_effects(
                tool, context, args
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
            effect_scope=potential_scope,
            potential_effects=[
                {
                    "path": path,
                    "before_state": state,
                    "before_artifact_id": preimages.get(path, ""),
                }
                for path, state in sorted(effects_before.items())
            ],
        )

        try:
            execution = self._invoke_runner(tool, context, args)
            outcome = self._result_outcome(
                call,
                execution,
                preimages,
                observation=tool.get("batchable_observation", False),
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            if tool.get("batchable_observation", False):
                outcome = self._observation_outcome(
                    PreparedObservation(call, tool, context), exc
                )
            else:
                effects_after = self._effect_snapshot(agent, potential_paths)
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
        safe_content = self.runtime.redact_text(content)
        safe_structured = redact_facts(dict(structured or {}), self.runtime.redact_text)
        if failure is not None:
            failure = FailureInfo(
                failure.code,
                self.runtime.redact_text(failure.detail),
                failure.recovery,
            )
        descriptor = {}
        if len(safe_content.encode("utf-8")) > DEFAULT_TOOL_PREVIEW_BYTES:
            descriptor = self.runtime.dependencies.artifacts.write_tool_output(
                _run_id(self.runtime), call.call_id, safe_content
            )
        return ToolOutcome(
            tool_call_id=call.call_id,
            tool_name=call.name,
            status=status,
            execution_state=execution_state,
            side_effect_state=side_effect_state,
            content=model_tool_output(safe_content, descriptor),
            structured=safe_structured,
            failure=failure,
            affected_paths=tuple(affected_paths),
            effect_scope=effect_scope if side_effect_state != "none" else "none",
            artifact_id=str(descriptor.get("artifact_id", "")),
        )
