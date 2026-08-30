"""Staged tool admission and canonical outcome construction."""

import json

from .contracts import (
    FailureInfo,
    ToolCall,
    ToolFailureError,
    ToolOutcome,
    ToolRunnerResult,
)

DEFAULT_TOOL_PREVIEW_BYTES = 12 * 1024
SHELL_TOOL_PREVIEW_BYTES = 16 * 1024


def _tool_preview_limit(tool_name):
    return SHELL_TOOL_PREVIEW_BYTES if tool_name == "run_shell" else DEFAULT_TOOL_PREVIEW_BYTES


def _complete_lines_within_budget(lines, budget, *, from_tail=False):
    selected = []
    used = 0
    candidates = reversed(lines) if from_tail else iter(lines)
    for line in candidates:
        encoded_size = len(line.encode("utf-8")) + (1 if selected else 0)
        if used + encoded_size > budget:
            break
        selected.append(line)
        used += encoded_size
    if from_tail:
        selected.reverse()
    return selected


def model_tool_output(content, tool_name, descriptor):
    content = str(content)
    total_bytes = len(content.encode("utf-8"))
    limit = _tool_preview_limit(tool_name)
    if total_bytes <= limit:
        return content
    if not descriptor.get("artifact_id"):
        raise RuntimeError("truncated tool output requires an artifact")
    lines = content.splitlines()
    from_tail = tool_name == "run_shell"
    selected = _complete_lines_within_budget(lines, limit - 512, from_tail=from_tail)
    if from_tail:
        start_line = len(lines) - len(selected) + 1
        end_line = len(lines)
    else:
        start_line = 1
        end_line = len(selected)
    preview = "\n".join(selected)
    notice = (
        f"[Output truncated: showing lines {start_line}-{end_line} of {len(lines)}; "
        f"full_bytes={total_bytes}; artifact_id={descriptor['artifact_id']}. "
        "Use read_artifact with this artifact_id and offset=0 to inspect the full "
        "output in 8 KiB pages.]"
    )
    return "\n".join(part for part in (preview, notice) if part)


def _run_id(agent):
    return str(agent.run.projection.run_id or "manual")


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent
        self._repeat_outcomes = {}

    @staticmethod
    def _run_boundary_reason(agent, call_id):
        task = agent.run.task
        if task is None:
            recovery_state = getattr(getattr(agent, "recovery", None), "state", None)
            recovery_status = (
                recovery_state.get("status", "")
                if isinstance(recovery_state, dict)
                else getattr(recovery_state, "status", "")
            )
            if recovery_status == "resumable":
                return "a resumable Run must be resumed or reset before manual tools"
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
        reason = self._run_boundary_reason(self.agent, call.call_id)
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
        tool = self.agent.tools.registry.get(call.name)
        if tool is None:
            return None, self._rejected(
                call, "unknown_tool", "unknown tool", "retry_after_change"
            )
        allowed = self.agent.config.allowed_tools
        if allowed is not None and call.name not in allowed:
            return None, self._rejected(
                call, "tool_not_allowed", "tool outside run surface"
            )
        if self.agent.run.task is None and not tool.get(
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
    def _redact_structured(cls, agent, value):
        if isinstance(value, str):
            return agent.redact_text(value)
        if isinstance(value, dict):
            return {
                str(key): cls._redact_structured(agent, item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._redact_structured(agent, item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return agent.redact_text(str(value))

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

    @staticmethod
    def _repeat_key(run_id, name, args):
        try:
            args_signature = json.dumps(
                dict(args),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return None
        return str(run_id), str(name), args_signature

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
    def _tracked_workspace_drift(agent, states, effect_scope):
        if effect_scope not in {"workspace", "mixed"}:
            return ()
        evidence = getattr(getattr(agent.run, "projection", None), "evidence", None)
        evidence = evidence or getattr(agent.run, "evidence", None)
        tracked = getattr(getattr(evidence, "change_set", None), "files", {})
        drift = []
        for path, actual_state in sorted(states.items()):
            change = tracked.get(path)
            if change is None:
                continue
            projected_state = str(change.current_after_state)
            if projected_state != actual_state:
                drift.append(
                    {
                        "path": path,
                        "projected_state": projected_state,
                        "actual_state": actual_state,
                    }
                )
        return tuple(drift)

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

    @staticmethod
    def _attach_preimage_artifacts(structured, preimages):
        structured = dict(structured or {})
        transitions = []
        for item in structured.get("path_transitions", ()):
            transition = dict(item)
            path = str(transition.get("path", ""))
            transition["before_artifact_id"] = str(
                preimages[path]
                if path in preimages
                else transition.get("before_artifact_id", "")
            )
            transitions.append(transition)
        if transitions:
            structured["path_transitions"] = transitions
        return structured

    @staticmethod
    def _effect_diff(before, after):
        return [
            path
            for path in sorted(set(before) | set(after))
            if before.get(path, "absent") != after.get(path, "absent")
        ]

    @staticmethod
    def _path_transitions(before, after, preimages, paths):
        return [
            {
                "path": path,
                "before_state": before[path],
                "after_state": after[path],
                "before_artifact_id": preimages.get(path, ""),
            }
            for path in paths
        ]

    def execute(self, call):
        agent = self.agent
        name, args = call.name, call.args
        boundary_rejection = self._reject_out_of_protocol_call(call)
        if boundary_rejection is not None:
            return boundary_rejection
        run_id = _run_id(agent)
        tool, admission_rejection = self._resolve_tool(call)
        if admission_rejection is not None:
            return admission_rejection
        workspace_mutating = bool(tool.get("workspace_mutating", False))
        raw_key = self._repeat_key(run_id, name, args)
        repeated = self._reject_repeated_call(call, raw_key)
        if repeated is not None:
            return repeated

        try:
            args = agent.tools.validate(name, args)
        except ToolFailureError as exc:
            return self._rejected(
                call,
                exc.failure.code,
                exc.failure.detail,
                exc.failure.recovery,
                structured=exc.structured,
            )
        except Exception as exc:  # noqa: BLE001 - validator boundary
            return self._rejected(
                call,
                "invalid_arguments",
                str(exc),
                "retry_after_change",
            )
        call = ToolCall(name, args, call.call_id)
        agent.prompt.refresh()
        repeat_key = self._repeat_key(run_id, name, args)
        repeated = self._reject_repeated_call(call, repeat_key)
        if repeated is not None:
            return repeated
        if tool["risky"] and not agent.tools.approve(name, args):
            return self._rejected(call, "approval_denied", "approval denied")

        try:
            potential_scope, potential_paths = self._potential_effects(tool, args)
            effects_before = self._effect_snapshot(agent, potential_paths)
        except Exception as exc:  # noqa: BLE001 - fail before side effect
            return self._rejected(
                call, "effect_planning_failed", str(exc), "retry_after_change"
            )
        drift = self._tracked_workspace_drift(
            agent,
            effects_before,
            potential_scope,
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
            paths = list(execution.affected_paths)
            failure = execution.failure
            status = "success" if failure is None else ("partial_success" if paths else "error")
            side_effect = (
                "partial" if failure is not None and paths else ("changed" if paths else "none")
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
                structured=self._attach_preimage_artifacts(
                    execution.structured, preimages
                ),
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            effects_after = self._effect_snapshot(agent, potential_paths)
            detected_paths = self._effect_diff(effects_before, effects_after)
            typed_error = exc if isinstance(exc, ToolFailureError) else None
            observed_workspace_drift = bool(
                typed_error
                and detected_paths
                and potential_scope in {"workspace", "mixed"}
            )
            paths = [] if typed_error else detected_paths
            unknown = bool(not typed_error and workspace_mutating and not potential_paths)
            uncertain = bool(paths or unknown)
            transitions = self._path_transitions(
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
        if repeat_key is not None and outcome.side_effect_state in {"partial", "unknown"}:
            self._repeat_outcomes.setdefault(repeat_key, []).append(outcome)
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
            self._record_tool_result(self.agent, outcome)
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
        safe_content = self.agent.redact_text(content)
        safe_structured = self._redact_structured(
            self.agent, dict(structured or {})
        )
        if failure is not None:
            failure = FailureInfo(
                failure.code,
                self.agent.redact_text(failure.detail),
                failure.recovery,
            )
        descriptor = {}
        if len(safe_content.encode("utf-8")) > _tool_preview_limit(call.name):
            descriptor = self.agent.dependencies.artifacts.write_tool_output(
                _run_id(self.agent), call.call_id, safe_content
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
            artifact=descriptor,
        )
