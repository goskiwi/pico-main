"""Staged tool admission and canonical outcome construction."""

import time
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
    limit = SHELL_TOOL_PREVIEW_BYTES if tool_name == "run_shell" else DEFAULT_TOOL_PREVIEW_BYTES
    if total_bytes <= limit:
        return content, False
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
    return "\n".join(part for part in (preview, notice) if part), True


class ToolExecutor:
    STAGES = ("registry", "surface", "schema", "policy", "approval")

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
        call_hash,
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
                tool_call_hash=call_hash,
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

    @classmethod
    def _call_state(cls, agent, name, args):
        paths = []
        working_state = ()
        if name in {"read_file", "write_file", "patch_file"}:
            paths.append(agent.workspace.resolve_path(args["path"]))
        elif name in {"list_files", "search"}:
            paths.append(agent.workspace.resolve_path(args.get("path", ".")))
        elif name in {"memory_store", "memory_forget"}:
            memory = agent.services.project_memory
            paths.extend((memory.cards_root / args["filename"], memory.index_path))
        elif name == "memory_recall":
            memory = agent.services.project_memory
            paths.extend(memory.cards_root / filename for filename in args["filenames"])
        elif name == "update_working_state" and agent.run.task_state is not None:
            state = agent.run.task_state.working_state
            working_state = (
                state.constraints,
                state.decisions,
                state.next_steps,
            )
        return (
            agent.workspace.revision,
            tuple(
                (path.as_posix(), agent.workspace.path_state(path))
                for path in paths
            ),
            working_state,
        )

    @staticmethod
    def _repeat_key(run_id, call_hash, state):
        return str(run_id), str(call_hash), state

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
            path = agent.workspace.resolve_path(args["path"])
            return "workspace", ((cls._logical_path(agent, path), path),)
        if name in {"memory_store", "memory_forget"}:
            memory = agent.services.project_memory
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
        started = time.monotonic()
        agent = self.agent
        name, args = call.name, call.args
        call_hash = tool_call_hash(name, args)

        tool = agent.tools.registry.get(name)
        if tool is None:
            return self._rejected(call, call_hash, "unknown_tool", "registry", "unknown tool", started)
        workspace_mutating = bool(tool.get("workspace_mutating", False))

        if (
            agent.config.allowed_tools is not None
            and name not in agent.config.allowed_tools
        ):
            return self._rejected(call, call_hash, "tool_not_allowed", "surface", "tool outside run surface", started)

        try:
            args = agent.tools.validate(name, args)
        except Exception as exc:  # noqa: BLE001 - admission converts validator failures to outcomes
            detail = str(exc)
            return self._rejected(
                call, call_hash, "invalid_arguments", "schema", detail, started,
            )
        call = ToolCall(name, args, call.call_id)
        call_hash = tool_call_hash(name, args)

        agent.prompt.refresh()
        run_id = agent.run.task_state.run_id if agent.run.task_state else "manual"
        call_state = self._call_state(agent, name, args)
        repeat_key = self._repeat_key(run_id, call_hash, call_state)
        repeat_reason = self._repeat_block_reason(self._outcomes_by_state.get(repeat_key, ()))
        if repeat_reason:
            return self._rejected(
                call, call_hash, "repeated_identical_call", "policy",
                repeat_reason, started,
            )
        if tool["risky"] and not agent.tools.approve(name, args):
            return self._rejected(
                call, call_hash, "approval_denied", "approval", "approval denied", started,
            )

        potential_scope, potential_paths = self._potential_effects(
            agent, name, args, workspace_mutating
        )
        effects_before = self._effect_snapshot(agent, potential_paths)
        self._record_tool_started(
            agent,
            call,
            call_hash=call_hash,
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
            artifact_content = execution.content
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
                side_effect, artifact_content, started, failure=failure, affected_paths=paths,
                effect_scope=effect_scope,
                artifact_content=artifact_content,
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary must capture arbitrary runner failures
            effects_after = self._effect_snapshot(agent, potential_paths)
            paths = self._effect_diff(effects_before, effects_after)
            unknown = bool(workspace_mutating and not potential_paths)
            uncertain = bool(paths or unknown)
            outcome = self._outcome(
                call, "partial_success" if uncertain else "error", "failed",
                "partial" if paths else ("unknown" if unknown else "none"),
                f"error: tool {name} failed: {exc}", started,
                failure=FailureInfo(
                    "tool_partial_success" if paths else ("tool_effect_unknown" if unknown else "tool_failed"),
                    "execution",
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
            agent.workspace.mark_changed()
            agent.prompt.refresh(force=True)
        self._record_tool_result(agent, outcome)
        result_key = self._repeat_key(
            run_id,
            call_hash,
            self._call_state(agent, name, args),
        )
        self._outcomes_by_state.setdefault(result_key, []).append(outcome)
        return outcome

    def _rejected(
        self, call, call_hash, code, stage, detail, started,
    ):
        outcome = self._outcome(
            call, "rejected", "not_started", "none",
            f"error: {detail} for {call.name}", started,
            failure=FailureInfo(code, "admission", detail, code == "repeated_identical_call"),
            rejected_at=stage,
        )
        self._record_tool_result(self.agent, outcome)
        return outcome

    def _outcome(
        self, call, status, execution_state, side_effect_state,
        content, started, *, failure=None, affected_paths=(), effect_scope="none",
        artifact_content=None,
        rejected_at="",
    ):
        run_id = (
            self.agent.run.task_state.run_id
            if self.agent.run.task_state
            else "manual"
        )
        descriptor = self.agent.services.artifacts.write_tool_output(
            run_id,
            call.call_id,
            call.name,
            content if artifact_content is None else artifact_content,
        )
        content, output_truncated = model_tool_output(
            content if artifact_content is None else artifact_content,
            call.name,
            descriptor,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
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
            duration_ms=duration_ms,
            artifact=descriptor,
            output_truncated=output_truncated,
            rejected_at=rejected_at,
        )
