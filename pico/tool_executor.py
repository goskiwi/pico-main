"""Staged tool admission and canonical outcome construction."""

from pathlib import Path

from .contracts import (
    FailureInfo,
    ToolCall,
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
    def _record_matching_verification(agent, call, outcome, result_entry):
        configured = str(agent.config.verification_command or "").strip()
        command = str(call.args.get("command", "")).strip()
        if (
            result_entry is None
            or call.name != "run_shell"
            or outcome.status != "success"
            or not configured
            or command != configured
        ):
            return
        fingerprint = agent.workspace.content_fingerprint(force=True)
        agent.emit_event(
            "verification_result",
            {
                "command": command,
                "status": "passed",
                "freshness": "current",
                "workspace_fingerprint": fingerprint,
                "exit_code": 0,
                "output": outcome.content[-4000:],
                "source_tool_call_id": call.call_id,
            },
        )

    @classmethod
    def _call_state(cls, agent, name, args):
        paths = []
        working_state = ()
        workspace_revision = None
        if name in {"read_file", "write_file", "patch_file"}:
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

    @staticmethod
    def _logical_path(agent, path):
        path = Path(path).resolve()
        try:
            return path.relative_to(agent.workspace.root).as_posix()
        except ValueError:
            return path.as_posix()

    @classmethod
    def _potential_effects(cls, agent, name, args, workspace_mutating):
        if name in {"write_file", "patch_file"}:
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
            retryable = (
                last.failure is not None
                and last.failure.retryable
            )
            if retryable and sum(item.status == "error" for item in previous) < 2:
                return ""
            return "same failed call already used its unchanged-state retry"
        return "same call already completed without a state change"

    def execute(self, call):
        agent = self.agent
        name, args = call.name, call.args

        tool = agent.tools.registry.get(name)
        if tool is None:
            return self._rejected(call, "unknown_tool", "unknown tool")
        workspace_mutating = bool(tool.get("workspace_mutating", False))

        if (
            agent.config.allowed_tools is not None
            and name not in agent.config.allowed_tools
        ):
            return self._rejected(
                call, "tool_not_allowed", "tool outside run surface"
            )

        try:
            args = agent.tools.validate(name, args)
        except Exception as exc:  # noqa: BLE001 - admission converts validator failures to outcomes
            detail = str(exc)
            return self._rejected(
                call, "invalid_arguments", detail
            )
        call = ToolCall(name, args, call.call_id)
        call_hash = tool_call_hash(name, args)

        agent.prompt.refresh()
        run_id = agent.run.task_state.run_id if agent.run.task_state else "manual"
        call_state = self._call_state(agent, name, args)
        repeat_key = (str(run_id), call_hash, call_state)
        repeat_reason = self._repeat_block_reason(self._outcomes_by_state.get(repeat_key, ()))
        if repeat_reason:
            return self._rejected(
                call, "repeated_identical_call", repeat_reason
            )
        if tool["risky"] and not agent.tools.approve(name, args):
            return self._rejected(
                call, "approval_denied", "approval denied"
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
        try:
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
                effect_scope=effect_scope,
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary must capture arbitrary runner failures
            effects_after = self._effect_snapshot(agent, potential_paths)
            paths = self._effect_diff(effects_before, effects_after)
            unknown = bool(workspace_mutating and not potential_paths)
            uncertain = bool(paths or unknown)
            outcome = self._outcome(
                call, "partial_success" if uncertain else "error", "failed",
                "partial" if paths else ("unknown" if unknown else "none"),
                f"error: tool {name} failed: {exc}",
                failure=FailureInfo(
                    "tool_partial_success" if paths else ("tool_effect_unknown" if unknown else "tool_failed"),
                    str(exc),
                    not uncertain,
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
        self._record_matching_verification(agent, call, outcome, result_entry)
        result_key = (
            str(run_id),
            call_hash,
            self._call_state(agent, name, args),
        )
        self._outcomes_by_state.setdefault(result_key, []).append(outcome)
        return outcome

    def _rejected(self, call, code, detail):
        outcome = self._outcome(
            call, "rejected", "not_started", "none",
            f"error: {detail} for {call.name}",
            failure=FailureInfo(
                code,
                detail,
                code == "repeated_identical_call",
            ),
        )
        self._record_tool_result(self.agent, outcome)
        return outcome

    def _outcome(
        self, call, status, execution_state, side_effect_state,
        content, *, failure=None, affected_paths=(), effect_scope="none",
    ):
        run_id = (
            self.agent.run.task_state.run_id
            if self.agent.run.task_state
            else "manual"
        )
        safe_content = self.agent.redact_text(content)
        if failure is not None:
            failure = FailureInfo(
                failure.code,
                self.agent.redact_text(failure.detail),
                failure.retryable,
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
        return ToolOutcome(
            tool_call_id=call.call_id,
            tool_name=call.name,
            status=status,
            execution_state=execution_state,
            side_effect_state=side_effect_state,
            content=content,
            failure=failure,
            affected_paths=tuple(affected_paths),
            effect_scope=effect_scope if side_effect_state != "none" else "none",
            artifact=descriptor,
        )
