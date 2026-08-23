"""Staged tool admission and canonical outcome construction."""

from pathlib import Path

from .contracts import (
    FailureInfo,
    ToolCall,
    ToolFailureError,
    ToolOutcome,
    ToolRunnerResult,
    tool_call_hash,
)

DEFAULT_TOOL_PREVIEW_BYTES = 12 * 1024
SHELL_TOOL_PREVIEW_BYTES = 16 * 1024


def _tool_preview_limit(tool_name):
    return (
        SHELL_TOOL_PREVIEW_BYTES
        if tool_name == "run_shell"
        else DEFAULT_TOOL_PREVIEW_BYTES
    )


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
        "Use read_artifact with this artifact_id and "
        "offset=0 to inspect the full output in 8 KiB pages.]"
    )
    return "\n".join(part for part in (preview, notice) if part)


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent
        self._outcomes_by_state = {}

    @staticmethod
    def _recorded_run_log(agent, call_id):
        run_log = agent.run.run_log
        if run_log is None:
            return None
        pending = run_log.pending_call_id()
        if not pending:
            return None
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
        cls,
        agent,
        call,
        *,
        risky,
        effect_scope,
        potential_effects,
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
        return agent.apply_run_event(
            run_log.append_tool_result(
                outcome,
                workspace_revision=agent.workspace.revision,
            )
        )

    @staticmethod
    def _is_matching_verification(agent, call):
        configured = str(agent.config.verification_command or "").strip()
        command = str(call.args.get("command", "")).strip()
        return bool(
            call.name == "run_shell"
            and configured
            and command == configured
        )

    @classmethod
    def _record_matching_verification(
        cls,
        agent,
        call,
        outcome,
        result_entry,
        started_fingerprint,
    ):
        if (
            result_entry is None
            or not started_fingerprint
            or not cls._is_matching_verification(agent, call)
        ):
            return
        finished_fingerprint = agent.workspace.content_fingerprint(force=True)
        stale = started_fingerprint != finished_fingerprint
        if stale:
            status = "stale"
        elif outcome.status == "success":
            status = "passed"
        elif (
            outcome.failure is not None
            and outcome.failure.code == "sandbox_infrastructure_error"
        ):
            status = "infrastructure_error"
        else:
            status = "failed"
        agent.emit_event(
            "verification_result",
            {
                "command": str(call.args.get("command", "")).strip(),
                "status": status,
                "freshness": "stale" if stale else "current",
                "started_workspace_fingerprint": started_fingerprint,
                "workspace_fingerprint": finished_fingerprint,
                "exit_code": outcome.structured.get("exit_code"),
                "output": outcome.content[-4000:],
                "source_tool_call_id": call.call_id,
            },
        )

    @classmethod
    def _call_state(cls, agent, name, args):
        paths = []
        working_state = ()
        workspace_revision = None
        if name in {"read_file", "write_file", "edit_file"}:
            paths.append(agent.workspace.resolve_tool_path(args["path"]))
        elif name in {"list_files", "search"}:
            paths.append(agent.workspace.resolve_tool_path(args.get("path", ".")))
            workspace_revision = agent.workspace.revision
        elif name == "run_shell":
            workspace_revision = agent.workspace.revision
        elif name in {"memory_store", "memory_forget"}:
            memory = agent.dependencies.project_memory
            paths.extend((memory.cards_root / args["filename"], memory.index_path))
        elif name == "memory_recall":
            memory = agent.dependencies.project_memory
            paths.extend(memory.cards_root / filename for filename in args["filenames"])
        elif name == "update_working_state" and agent.run.task_state is not None:
            state = agent.run.task_state.working_state
            working_state = (
                state.constraints,
                state.decisions,
                state.next_steps,
            )
        elif agent.tools.registry.get(name, {}).get("workspace_mutating", False):
            workspace_revision = agent.workspace.revision
        return (
            workspace_revision,
            tuple(
                (path.as_posix(), agent.workspace.path_state(path))
                for path in paths
            ),
            working_state,
        )

    @classmethod
    def _repeat_key(cls, agent, run_id, name, args):
        try:
            call_state = cls._call_state(agent, name, args)
            call_signature = tool_call_hash(name, args)
        except (KeyError, TypeError, ValueError):
            try:
                call_signature = tool_call_hash(name, args)
            except (TypeError, ValueError):
                return None
            call_state = (agent.workspace.revision, (), ())
        policy_state = (
            agent.config.approval_policy,
            agent.config.read_only,
            agent.config.allowed_tools,
        )
        return (
            str(run_id),
            call_signature,
            call_state,
            policy_state,
        )

    @staticmethod
    def _logical_path(agent, path):
        path = Path(path).resolve()
        try:
            return path.relative_to(agent.workspace.root).as_posix()
        except ValueError:
            return path.as_posix()

    @classmethod
    def _potential_effects(cls, agent, name, args, workspace_mutating):
        if name in {"write_file", "edit_file"}:
            path = agent.workspace.resolve_tool_path(args["path"])
            return "workspace", ((cls._logical_path(agent, path), path),)
        if name in {"memory_store", "memory_forget"}:
            memory = agent.dependencies.project_memory
            paths = (memory.cards_root / args["filename"], memory.index_path)
            return "project_memory", tuple(
                (cls._logical_path(agent, path), path) for path in paths
            )
        return ("workspace" if workspace_mutating else "none"), ()

    @staticmethod
    def _effect_snapshot(agent, paths):
        return {
            logical: agent.workspace.path_state(path)
            for logical, path in paths
        }

    @staticmethod
    def _effect_diff(before, after):
        paths = []
        for path in sorted(set(before) | set(after)):
            old = before.get(path, "absent")
            new = after.get(path, "absent")
            if old == new:
                continue
            paths.append(path)
        return paths

    @staticmethod
    def _repeat_block_reason(previous):
        if not previous:
            return ""
        last = previous[-1]
        if last.side_effect_state in {"partial", "unknown"}:
            return "same call previously left an uncertain side effect; inspect state before another action"
        if last.status == "success":
            if last.side_effect_state == "changed":
                return "same mutation already committed in the current state"
            return "same successful call already ran in the current state and produced no new evidence"
        if last.status == "error":
            recovery = (
                last.failure is not None
                and last.failure.recovery
            )
            if (
                recovery == "retry_after_wait"
                and sum(item.status == "error" for item in previous) < 2
            ):
                return ""
            if recovery == "retry_after_change":
                return "same failed call requires a changed parameter, workspace, or other precondition"
            if recovery == "user_action_required":
                return "same failed call requires user action before another attempt"
            return "same failed call has no unchanged-state retry route"
        return "same call already completed without a state change"

    @staticmethod
    def _correction_action(failure):
        if failure is None:
            return "continue"
        return {
            "retry_after_change": "repair",
            "retry_after_wait": "wait",
            "user_action_required": "request_user_action",
            "no_retry": "stop_route",
        }[failure.recovery]

    def execute(self, call):
        agent = self.agent
        name, args = call.name, call.args
        run_id = agent.run.task_state.run_id if agent.run.task_state else "manual"
        admission_key = self._repeat_key(agent, run_id, name, args)
        repeat_reason = self._repeat_block_reason(
            self._outcomes_by_state.get(admission_key, ())
            if admission_key is not None
            else ()
        )
        if repeat_reason:
            return self._rejected(
                call,
                "repeated_identical_call",
                repeat_reason,
                "retry_after_change",
                correction_action="replan",
                outcome_key=admission_key,
            )

        tool = agent.tools.registry.get(name)
        if tool is None:
            return self._rejected(
                call,
                "unknown_tool",
                "unknown tool",
                "retry_after_change",
                outcome_key=admission_key,
            )
        workspace_mutating = bool(tool.get("workspace_mutating", False))

        if (
            agent.config.allowed_tools is not None
            and name not in agent.config.allowed_tools
        ):
            return self._rejected(
                call,
                "tool_not_allowed",
                "tool outside run surface",
                outcome_key=admission_key,
            )

        try:
            args = agent.tools.validate(name, args)
        except ToolFailureError as exc:
            return self._rejected(
                call,
                exc.failure.code,
                exc.failure.detail,
                exc.failure.recovery,
                outcome_key=admission_key,
            )
        except Exception as exc:  # noqa: BLE001 - admission converts validator failures to outcomes
            detail = str(exc)
            return self._rejected(
                call,
                "invalid_arguments",
                detail,
                "retry_after_change",
                outcome_key=admission_key,
            )
        call = ToolCall(name, args, call.call_id)

        agent.prompt.refresh()
        repeat_key = self._repeat_key(agent, run_id, name, args)
        repeat_reason = self._repeat_block_reason(self._outcomes_by_state.get(repeat_key, ()))
        if repeat_reason:
            return self._rejected(
                call,
                "repeated_identical_call",
                repeat_reason,
                "retry_after_change",
                correction_action="replan",
                outcome_key=repeat_key,
            )
        if tool["risky"] and not agent.tools.approve(name, args):
            return self._rejected(
                call,
                "approval_denied",
                "approval denied",
                outcome_key=repeat_key,
            )

        potential_scope, potential_paths = self._potential_effects(
            agent, name, args, workspace_mutating
        )
        effects_before = self._effect_snapshot(agent, potential_paths)
        self._record_tool_started(
            agent,
            call,
            risky=bool(tool["risky"]),
            effect_scope=potential_scope,
            potential_effects=[
                {"path": path, "before_state": state}
                for path, state in sorted(effects_before.items())
            ],
        )
        verification_start_fingerprint = ""
        try:
            if self._is_matching_verification(agent, call):
                verification_start_fingerprint = (
                    agent.workspace.content_fingerprint(force=True)
                )
            execution = tool["run"](args)
            if not isinstance(execution, ToolRunnerResult):
                raise TypeError("tool runner must return ToolRunnerResult")
            paths = list(execution.affected_paths)
            effect_scope = execution.effect_scope
            failure = execution.failure
            status = (
                "success"
                if failure is None
                else ("partial_success" if paths else "error")
            )
            side_effect = (
                "partial"
                if failure is not None and paths
                else ("changed" if paths else "none")
            )
            outcome = self._outcome(
                call, status,
                "completed",
                side_effect, execution.content, failure=failure, affected_paths=paths,
                effect_scope=effect_scope, structured=execution.structured,
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary must capture arbitrary runner failures
            effects_after = self._effect_snapshot(agent, potential_paths)
            paths = self._effect_diff(effects_before, effects_after)
            unknown = bool(workspace_mutating and not potential_paths)
            uncertain = bool(paths or unknown)
            typed_failure = (
                exc.failure
                if isinstance(exc, ToolFailureError) and not uncertain
                else None
            )
            outcome = self._outcome(
                call, "partial_success" if uncertain else "error", "failed",
                "partial" if paths else ("unknown" if unknown else "none"),
                f"error: tool {name} failed: {exc}",
                failure=typed_failure or FailureInfo(
                    "tool_partial_success" if paths else ("tool_effect_unknown" if unknown else "tool_failed"),
                    str(exc),
                    "no_retry" if uncertain else "retry_after_change",
                ),
                affected_paths=paths,
                effect_scope=potential_scope,
            )

        workspace_effect = (
            outcome.side_effect_state != "none"
            and outcome.effect_scope in {"workspace", "mixed"}
        )
        if workspace_effect:
            revision_before_refresh = agent.workspace.revision
            agent.prompt.refresh(force=True)
            if agent.workspace.revision == revision_before_refresh:
                agent.workspace.mark_changed()
        result_entry = self._record_tool_result(agent, outcome)
        self._record_matching_verification(
            agent,
            call,
            outcome,
            result_entry,
            verification_start_fingerprint,
        )
        result_key = self._repeat_key(agent, run_id, name, args)
        self._outcomes_by_state.setdefault(result_key, []).append(outcome)
        return outcome

    def _rejected(
        self,
        call,
        code,
        detail,
        recovery="no_retry",
        *,
        correction_action=None,
        outcome_key=None,
    ):
        outcome = self._outcome(
            call, "rejected", "not_started", "none",
            f"error: {detail} for {call.name}",
            failure=FailureInfo(
                code,
                detail,
                recovery,
            ),
            correction_action=correction_action,
        )
        self._record_tool_result(self.agent, outcome)
        if outcome_key is not None:
            self._outcomes_by_state.setdefault(outcome_key, []).append(outcome)
        return outcome

    def _outcome(
        self, call, status, execution_state, side_effect_state,
        content, *, failure=None, affected_paths=(), effect_scope="none",
        structured=None, correction_action=None,
    ):
        run_id = (
            self.agent.run.task_state.run_id
            if self.agent.run.task_state
            else "manual"
        )
        safe_content = self.agent.redact_text(content)
        safe_structured = self._redact_structured(
            self.agent,
            dict(structured or {}),
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
                run_id,
                call.call_id,
                safe_content,
            )
        content = model_tool_output(
            safe_content,
            call.name,
            descriptor,
        )
        correction_action = (
            self._correction_action(failure)
            if correction_action is None
            else str(correction_action)
        )
        return ToolOutcome(
            tool_call_id=call.call_id,
            tool_name=call.name,
            status=status,
            execution_state=execution_state,
            side_effect_state=side_effect_state,
            content=content,
            correction_action=correction_action,
            structured=safe_structured,
            failure=failure,
            affected_paths=tuple(affected_paths),
            effect_scope=effect_scope if side_effect_state != "none" else "none",
            artifact=descriptor,
        )
