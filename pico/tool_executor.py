"""Staged tool admission and canonical outcome construction."""

import re
import time
from pathlib import Path

from .contracts import (
    FailureInfo,
    ToolAttempt,
    ToolCall,
    ToolExecution,
    ToolOutcome,
    canonical_fingerprint,
)
from .hooks import BeforeToolContext
from .mutations import file_revision
from .recovery import RecoveryPolicy

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
        self.recovery_policy = RecoveryPolicy()

    @staticmethod
    def _path_state(path):
        path = Path(path)
        try:
            if path.is_file():
                return file_revision(path)
            if path.is_dir():
                stat = path.stat()
                return f"dir:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
        except OSError:
            return "unavailable"
        return "absent"

    @classmethod
    def _call_state(cls, agent, name, args):
        paths = []
        if name in {"read_file", "write_file", "patch_file"}:
            paths.append(agent.workspace.resolve_path(args["path"]))
        elif name in {"list_files", "search"}:
            paths.append(agent.workspace.resolve_path(args.get("path", ".")))
        elif name in {"memory_store", "memory_forget"}:
            memory = agent.services.project_memory
            paths.extend((memory.cards_root / args["filename"], memory.index_path))
        return (
            agent.workspace.revision,
            tuple((path.as_posix(), cls._path_state(path)) for path in paths),
        )

    @staticmethod
    def _repeat_key(run_id, call_fingerprint, state):
        return str(run_id), str(call_fingerprint), state

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

    @classmethod
    def _effect_snapshot(cls, paths):
        return {logical: cls._path_state(path) for logical, path in paths}

    @staticmethod
    def _effect_diff(before, after):
        paths = []
        summaries = []
        for path in sorted(set(before) | set(after)):
            old = before.get(path, "absent")
            new = after.get(path, "absent")
            if old == new:
                continue
            paths.append(path)
            if old == "absent":
                summaries.append(f"created:{path}")
            elif new == "absent":
                summaries.append(f"deleted:{path}")
            else:
                summaries.append(f"modified:{path}")
        return paths, summaries

    @staticmethod
    def _repeat_block_reason(previous):
        if not previous:
            return ""
        last = previous[-1]
        if last.side_effect_state in {"partial", "unknown"}:
            return "same call previously left an uncertain side effect; inspect state before another action"
        if last.status == "ok":
            if last.side_effect_state == "changed":
                return "same mutation already committed in the current state"
            return "same successful call already ran in the current state and produced no new evidence"
        if last.status == "error":
            retryable = (
                last.failure is not None
                and last.failure.retryable
                and last.recovery is not None
                and last.recovery.action == "retry"
            )
            if retryable and sum(item.status == "error" for item in previous) < 2:
                return ""
            return "same failed call already used its unchanged-state retry"
        return "same call already completed without a state change"

    def execute(self, call):
        started = time.monotonic()
        agent = self.agent
        name, args = call.name, call.args
        fingerprint = canonical_fingerprint(name, args)
        admission = {"status": "checking", "stages": []}

        tool = agent.tools.registry.get(name)
        if tool is None:
            return self._rejected(call, fingerprint, admission, "unknown_tool", "registry", "unknown tool", started)
        admission["stages"].append({"stage": "registry", "status": "passed"})
        workspace_mutating = bool(tool.get("workspace_mutating", False))

        if (
            agent.config.allowed_tools is not None
            and name not in agent.config.allowed_tools
        ):
            return self._rejected(call, fingerprint, admission, "tool_not_allowed", "surface", "tool outside run surface", started)
        admission["stages"].append({"stage": "surface", "status": "passed"})

        try:
            args = agent.tools.validate(name, args)
        except Exception as exc:  # noqa: BLE001 - admission converts validator failures to outcomes
            detail = str(exc)
            return self._rejected(
                call, fingerprint, admission, "invalid_arguments", "schema", detail, started,
                security_event_type="path_escape" if "path escapes workspace" in detail else "",
            )
        call = ToolCall(name, args, call.call_id)
        fingerprint = canonical_fingerprint(name, args)
        admission["stages"].append({"stage": "schema", "status": "passed"})

        agent.prompt.refresh()
        run_id = agent.run.task_state.run_id if agent.run.task_state else "manual"
        call_state = self._call_state(agent, name, args)
        repeat_key = self._repeat_key(run_id, fingerprint, call_state)
        repeat_reason = self._repeat_block_reason(self._outcomes_by_state.get(repeat_key, ()))
        if repeat_reason:
            return self._rejected(
                call, fingerprint, admission, "repeated_identical_call", "policy",
                repeat_reason, started,
            )
        hook_decision = agent.services.hooks.before_tool_call(
            BeforeToolContext(
                call=call,
                run_id=str(run_id),
                task_id=str(getattr(agent.run.task_state, "task_id", "manual")),
            )
        )
        if hook_decision.block or hook_decision.stop:
            return self._rejected(
                call,
                fingerprint,
                admission,
                "hook_blocked",
                "policy",
                hook_decision.reason or "runtime policy hook blocked the tool",
                started,
                policy_stop=hook_decision.stop,
            )
        admission["stages"].append({"stage": "policy", "status": "passed"})

        if tool["risky"] and not agent.tools.approve(name, args):
            return self._rejected(
                call, fingerprint, admission, "approval_denied", "approval", "approval denied", started,
                security_event_type=(
                    "read_only_block" if agent.config.read_only else "approval_denied"
                ),
            )
        admission["stages"].append({"stage": "approval", "status": "passed"})
        admission["status"] = "admitted"

        potential_scope, potential_paths = self._potential_effects(
            agent, name, args, workspace_mutating
        )
        effects_before = self._effect_snapshot(potential_paths)
        agent.emit_event(
            agent.run.task_state,
            "tool_started",
            {
                "tool_call_id": call.call_id,
                "tool_name": name,
                "call_fingerprint": fingerprint,
                "risky": bool(tool["risky"]),
                "effect_scope": potential_scope,
                "potential_effects": [
                    {"path": path, "before_state": state}
                    for path, state in sorted(effects_before.items())
                ],
            },
        )
        try:
            execution = tool["run"](args)
            if not isinstance(execution, ToolExecution):
                raise TypeError("tool runner must return ToolExecution")
            artifact_content = execution.content
            paths = list(execution.affected_paths)
            diff = list(execution.diff_summary)
            effect_scope = execution.effect_scope
            status, failure = self._classify_result(name, artifact_content, bool(paths))
            side_effect = "partial" if status == "partial_success" else ("changed" if paths else "none")
            outcome = self._outcome(
                call, fingerprint, admission, status,
                "completed" if status in {"ok", "partial_success"} else "failed",
                side_effect, artifact_content, started, failure=failure, affected_paths=paths,
                diff_summary=diff, risky=tool["risky"], effect_scope=effect_scope,
                artifact_content=artifact_content,
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary must capture arbitrary runner failures
            effects_after = self._effect_snapshot(potential_paths)
            paths, diff = self._effect_diff(effects_before, effects_after)
            unknown = bool(workspace_mutating and not potential_paths)
            uncertain = bool(paths or unknown)
            outcome = self._outcome(
                call, fingerprint, admission, "partial_success" if uncertain else "error", "failed",
                "partial" if paths else ("unknown" if unknown else "none"),
                f"error: tool {name} failed: {exc}", started,
                failure=FailureInfo(
                    "tool_partial_success" if paths else ("tool_effect_unknown" if unknown else "tool_failed"),
                    "execution",
                    str(exc),
                    not uncertain,
                ),
                affected_paths=paths, diff_summary=diff, risky=tool["risky"],
                effect_scope=potential_scope,
                security_event_type="path_escape" if "path escapes workspace" in str(exc) else "",
            )

        workspace_effect = (
            outcome.side_effect_state != "none"
            and outcome.metadata.get("effect_scope") in {"workspace", "mixed"}
        )
        if workspace_effect:
            agent.workspace.mark_changed()
            agent.prompt.refresh(force=True)
        result_key = self._repeat_key(
            run_id,
            fingerprint,
            self._call_state(agent, name, args),
        )
        self._outcomes_by_state.setdefault(result_key, []).append(outcome)
        event = agent.emit_event(
            agent.run.task_state,
            "tool_result",
            {
                "tool_call_id": call.call_id,
                "tool_name": name,
                "workspace_revision": agent.workspace.revision,
                "outcome": outcome.to_dict(),
            },
        )
        agent.run.evidence.apply_entry(event)
        return outcome

    @staticmethod
    def _classify_result(name, content, changed):
        if name != "run_shell":
            return "ok", None
        match = re.search(r"exit_code:\s*(-?\d+)", content)
        if not match or int(match.group(1)) == 0:
            return "ok", None
        status = "partial_success" if changed else "error"
        code = "tool_partial_success" if changed else "tool_failed"
        return status, FailureInfo(code, "command", "shell command returned non-zero", True)

    def _rejected(
        self, call, fingerprint, admission, code, stage, detail, started,
        security_event_type="", policy_stop=False,
    ):
        admission["stages"].append({"stage": stage, "status": "rejected", "code": code})
        admission["status"] = "rejected"
        outcome = self._outcome(
            call, fingerprint, admission, "rejected", "not_started", "none",
            f"error: {detail} for {call.name}", started,
            failure=FailureInfo(code, "admission", detail, code == "repeated_identical_call"),
            risky=True, security_event_type=security_event_type,
            policy_stop=policy_stop,
        )
        self.agent.emit_event(
            self.agent.run.task_state,
            "tool_result",
            {"tool_call_id": call.call_id, "tool_name": call.name, "outcome": outcome.to_dict()},
        )
        return outcome

    def _outcome(
        self, call, fingerprint, admission, status, execution_state, side_effect_state,
        content, started, *, failure=None, affected_paths=(), diff_summary=(), risky=False,
        security_event_type="", effect_scope="workspace", artifact_content=None,
        policy_stop=False,
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
        recovery = (
            self.recovery_policy.assess(
                failure,
                status=status,
                fingerprint=fingerprint,
                scope=run_id,
            )
            if failure is not None
            else None
        )
        attempts = ()
        if execution_state != "not_started":
            attempts = (
                ToolAttempt(
                    1,
                    status,
                    execution_state,
                    side_effect_state,
                    duration_ms,
                    tuple(affected_paths),
                ),
            )
        return ToolOutcome(
            tool_call_id=call.call_id,
            tool_name=call.name,
            status=status,
            execution_state=execution_state,
            side_effect_state=side_effect_state,
            content=content,
            call_fingerprint=fingerprint,
            admission=admission,
            failure=failure,
            recovery=recovery,
            attempts=attempts,
            affected_paths=tuple(affected_paths),
            diff_summary=tuple(diff_summary),
            workspace_fingerprint=self.agent.workspace.context.fingerprint(),
            duration_ms=duration_ms,
            artifact_id=descriptor["artifact_id"],
            artifact=descriptor,
            metadata={
                "security_event_type": security_event_type,
                "risk_level": "high" if risky else "low",
                "read_only": not risky,
                "effect_scope": effect_scope if side_effect_state != "none" else "none",
                "output_truncated": output_truncated,
                "policy_stop_requested": bool(policy_stop),
            },
        )
