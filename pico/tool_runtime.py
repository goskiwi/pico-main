"""The one public runtime boundary for model-visible tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from . import tools as toolkit
from .contracts import (
    FailureInfo,
    ToolCall,
    ToolFailureError,
    ToolOutcome,
    ToolRunnerResult,
)
from .tool_context import ToolContext
from .tool_execution import (
    _tool_preview_limit,
    attach_preimage_artifacts,
    classify_runner_result,
    effect_diff,
    intersect_write_scopes,
    model_tool_output,
    path_transitions,
    redact_structured,
    repeat_key,
    tracked_workspace_drift,
)

if TYPE_CHECKING:
    from .runtime import Pico


def _run_id(agent):
    return str(agent.run.projection.run_id or "manual")


class ToolRuntime:
    """Own tool discovery, policy admission, and transaction execution."""

    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.registry = self._build_registry()
        self.surface = self._apply_allowlist(self.registry)
        self.action_schemas = toolkit.build_action_tools(self.surface)
        self._repeat_outcomes = {}

    def _build_registry(self):
        tools = toolkit.build_tool_registry(self.context())
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

    def validate(self, name, args):
        runtime = self.runtime
        task = runtime.run.task
        tool = self.registry.get(name)
        contract = getattr(task, "contract", None)
        if (
            contract is not None
            and contract.task_kind == "read_only"
            and tool is not None
            and tool.get("state_mutating", False)
        ):
            raise ToolFailureError(
                "read_only_task",
                f"task requirements do not allow state-mutating tool: {name}",
                "no_retry",
            )
        if tool is None:
            raise ValueError(f"unknown tool: {name}")
        validated = tool["args_schema"].model_validate(args or {}).model_dump()
        validator = tool.get("validate")
        if validator is not None:
            validated = validator(validated)
        if (
            contract is not None
            and contract.task_kind == "read_only"
            and name == "delegate_tasks"
            and any(item.get("kind") == "implement" for item in validated["tasks"])
        ):
            raise ToolFailureError(
                "read_only_task",
                "read-only task may delegate explore tasks only",
                "no_retry",
            )
        allowed_paths = self._effective_write_scope()
        if name in {"write_file", "edit_file"} and allowed_paths is not None:
            target = runtime.workspace.resolve_tool_path(validated["path"])
            relative = target.relative_to(runtime.workspace.root).as_posix()
            self._require_write_scope((relative,), allowed_paths)
        if name == "delegate_tasks" and allowed_paths is not None:
            declared_paths = tuple(
                path
                for task_spec in validated["tasks"]
                if task_spec["kind"] == "implement"
                for path in task_spec["allowed_write_paths"]
            )
            self._require_write_scope(declared_paths, allowed_paths)
        if name == "apply_task_patches" and allowed_paths is not None:
            _scope, planned_paths = self._potential_effects(tool, validated)
            self._require_write_scope(
                (logical for logical, _path in planned_paths),
                allowed_paths,
            )
        return validated

    def _effective_write_scope(self):
        task = self.runtime.run.task
        contract = getattr(task, "contract", None)
        return intersect_write_scopes(
            getattr(contract, "allowed_write_paths", None),
            self.runtime.config.allowed_write_paths,
        )

    @staticmethod
    def _require_write_scope(paths, allowed_paths):
        if allowed_paths is None:
            return
        outside = sorted(set(paths) - set(allowed_paths))
        if outside:
            raise ValueError(
                "write path outside allowed scope: " + ", ".join(outside)
            )

    def model_action_tools(self):
        task = self.runtime.run.task
        contract = getattr(task, "contract", None)
        if contract is None or contract.task_kind != "read_only":
            return list(self.action_schemas)
        return [
            schema
            for schema in self.action_schemas
            if schema["name"] == "submit_final"
            or not self.registry.get(schema["name"], {}).get(
                "state_mutating", False
            )
        ]

    def context(self):
        runtime = self.runtime
        return ToolContext(
            workspace_root=runtime.workspace.root,
            path_resolver=runtime.workspace.resolve_tool_path,
            shell_env_provider=runtime.shell_env,
            project_memory=runtime.dependencies.project_memory,
            artifact_store=runtime.dependencies.artifacts,
            run_id_provider=lambda: str(runtime.run.projection.run_id or "manual"),
            tool_call_id_provider=lambda: (
                runtime.run.run_log.pending_call_id()
                if runtime.run.run_log is not None
                else ""
            ),
            working_state_provider=lambda: (
                runtime.run.task.working if runtime.run.task is not None else None
            ),
            token_counter_provider=lambda text: runtime.prompt.context.tokenizer.count(
                text
            ),
            mutation_service=runtime.dependencies.mutations,
            sandbox=runtime.dependencies.sandbox,
            execution_context_provider=lambda: (
                runtime.run.execution_context.child()
                if runtime.run.execution_context is not None
                else None
            ),
        )

    def approve(self, name, args):
        config = self.runtime.config
        if config.approval_policy == "auto":
            return True
        if config.approval_policy == "deny":
            return False
        try:
            answer = input(
                f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] "
            )
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def _run_boundary_reason(agent, call_id):
        if agent.run.reload_required or agent.run.resumable:
            return "a resumable Run must be resumed or reset before manual tools"
        task = agent.run.task
        if task is None:
            return ""
        run_log = agent.run.run_log
        if run_log is None:
            return "active Run tool execution requires its Run Log"
        if agent.run.projection.terminal:
            return "terminal Run cannot execute additional tools"
        pending = run_log.pending_call_id()
        if not pending:
            return "active Run tools require a persisted assistant_tool_call"
        if pending != str(call_id):
            return "tool execution does not match the pending Run call"
        return ""

    def _reject_out_of_protocol_call(self, call):
        reason = self._run_boundary_reason(self.runtime, call.call_id)
        if not reason:
            return None
        return self._rejected(
            call,
            "run_protocol_violation",
            reason,
            "no_retry",
            record=False,
        )

    def _resolve_tool(self, call):
        tool = self.registry.get(call.name)
        if tool is None:
            return None, self._rejected(
                call, "unknown_tool", "unknown tool", "retry_after_change"
            )
        allowed = self.runtime.config.allowed_tools
        if allowed is not None and call.name not in allowed:
            return None, self._rejected(
                call, "tool_not_allowed", "tool outside run surface"
            )
        if self.runtime.run.task is None and not tool.get(
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
    def _record_tool_started(
        cls, agent, call, *, risky, effect_scope, potential_effects
    ):
        run_log = cls._recorded_run_log(agent, call.call_id)
        if run_log is None:
            return None
        return agent.apply_run_event(
            run_log.append_tool_started(
                call,
                risky=risky,
                effect_scope=effect_scope,
                potential_effects=potential_effects,
            )
        )

    @classmethod
    def _record_tool_result(cls, agent, outcome):
        run_log = cls._recorded_run_log(agent, outcome.tool_call_id)
        if run_log is None:
            return None
        return agent.apply_run_event(run_log.append_tool_result(outcome))

    def _reject_repeated_call(self, call, repeat_key):
        previous = self._repeat_outcomes.get(repeat_key, ()) if repeat_key else ()
        if not previous or previous[-1].side_effect_state not in {"partial", "unknown"}:
            return None
        return self._rejected(
            call,
            "repeated_identical_call",
            "same call previously left an uncertain side effect; inspect state before another action",
            "retry_after_change",
        )

    @staticmethod
    def _potential_effects(tool, args):
        planner = tool.get("potential_effects")
        if planner is not None:
            return planner(args)
        return (
            "workspace" if tool.get("workspace_mutating", False) else "none"
        ), ()

    @staticmethod
    def _effect_snapshot(agent, paths):
        return {
            logical: agent.workspace.path_state(path)
            for logical, path in paths
        }

    @staticmethod
    def _preimage_artifacts(agent, call, paths, states, effect_scope):
        if (
            effect_scope not in {"workspace", "mixed"}
            or agent.run.run_log is None
            or agent.run.task is None
        ):
            return {}
        evidence = getattr(getattr(agent.run, "projection", None), "evidence", None)
        evidence = evidence or getattr(agent.run, "evidence", None)
        existing_changes = getattr(getattr(evidence, "change_set", None), "files", {})
        artifacts = {}
        for logical, path in paths:
            before_state = states.get(logical, "absent")
            if before_state == "absent" or logical in existing_changes:
                artifacts[logical] = ""
                continue
            if not path.is_file():
                raise ValueError(f"workspace preimage is not a file: {logical}")
            descriptor = agent.dependencies.artifacts.write_workspace_preimage(
                _run_id(agent),
                call.call_id,
                logical,
                path.read_text(encoding="utf-8"),
            )
            artifacts[logical] = descriptor["artifact_id"]
        return artifacts

    def _validate_call(self, call):
        try:
            args = self.validate(call.name, call.args)
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

    def execute(self, call_or_name, args=None):
        call = (
            call_or_name
            if isinstance(call_or_name, ToolCall)
            else ToolCall(str(call_or_name), dict(args or {}))
        )
        agent = self.runtime
        name, args = call.name, call.args
        boundary_rejection = self._reject_out_of_protocol_call(call)
        if boundary_rejection is not None:
            return boundary_rejection
        run_id = _run_id(agent)
        tool, admission_rejection = self._resolve_tool(call)
        if admission_rejection is not None:
            return admission_rejection
        workspace_mutating = bool(tool.get("workspace_mutating", False))
        raw_key = repeat_key(run_id, name, args)
        repeated = self._reject_repeated_call(call, raw_key)
        if repeated is not None:
            return repeated

        call, validation_rejection = self._validate_call(call)
        if validation_rejection is not None:
            return validation_rejection
        args = call.args
        agent.prompt.refresh()
        normalized_repeat_key = repeat_key(run_id, name, args)
        repeated = self._reject_repeated_call(call, normalized_repeat_key)
        if repeated is not None:
            return repeated
        if tool["risky"] and not self.approve(name, args):
            return self._rejected(call, "approval_denied", "approval denied")

        try:
            potential_scope, potential_paths = self._potential_effects(tool, args)
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
        except Exception as exc:  # noqa: BLE001 - fail before side effect
            return self._rejected(
                call, "effect_planning_failed", str(exc), "retry_after_change"
            )
        self._record_tool_started(
            agent,
            call,
            risky=bool(tool["risky"]),
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

        observed_workspace_drift = False
        try:
            execution = tool["run"](args)
            if not isinstance(execution, ToolRunnerResult):
                raise TypeError("tool runner must return ToolRunnerResult")
            failure = execution.failure
            status, side_effect, paths = classify_runner_result(
                failure,
                execution.affected_paths,
            )
            outcome = self._outcome(
                call,
                status,
                "completed",
                side_effect,
                execution.content,
                failure=failure,
                affected_paths=paths,
                effect_scope=execution.effect_scope,
                structured=attach_preimage_artifacts(
                    execution.structured, preimages
                ),
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            effects_after = self._effect_snapshot(agent, potential_paths)
            detected_paths = effect_diff(effects_before, effects_after)
            typed_error = exc if isinstance(exc, ToolFailureError) else None
            observed_workspace_drift = bool(
                typed_error
                and detected_paths
                and potential_scope in {"workspace", "mixed"}
            )
            paths = [] if typed_error else detected_paths
            unknown = bool(not typed_error and workspace_mutating and not potential_paths)
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

        if (
            outcome.side_effect_state != "none"
            and outcome.effect_scope in {"workspace", "mixed"}
        ) or observed_workspace_drift:
            agent.prompt.refresh(force=True)
        self._record_tool_result(agent, outcome)
        if normalized_repeat_key is not None and outcome.side_effect_state in {
            "partial",
            "unknown",
        }:
            self._repeat_outcomes.setdefault(normalized_repeat_key, []).append(outcome)
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
        safe_structured = redact_structured(
            dict(structured or {}), self.runtime.redact_text
        )
        if failure is not None:
            failure = FailureInfo(
                failure.code,
                self.runtime.redact_text(failure.detail),
                failure.recovery,
            )
        descriptor = {}
        if len(safe_content.encode("utf-8")) > _tool_preview_limit(call.name):
            descriptor = self.runtime.dependencies.artifacts.write_tool_output(
                _run_id(self.runtime), call.call_id, safe_content
            )
        return ToolOutcome(
            tool_call_id=call.call_id,
            tool_name=call.name,
            status=status,
            execution_state=execution_state,
            side_effect_state=side_effect_state,
            content=model_tool_output(safe_content, call.name, descriptor),
            structured=safe_structured,
            failure=failure,
            affected_paths=tuple(affected_paths),
            effect_scope=effect_scope if side_effect_state != "none" else "none",
            artifact_id=str(descriptor.get("artifact_id", "")),
        )
