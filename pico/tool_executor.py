"""Staged tool admission and canonical outcome construction."""

import hashlib
import json
import re
import time

from .contracts import (
    FailureInfo,
    ToolAttempt,
    ToolCall,
    ToolOutcome,
    canonical_fingerprint,
)
from .recovery import RecoveryPolicy
from .workspace import MAX_TOOL_OUTPUT


def bounded_tool_output(content, limit=MAX_TOOL_OUTPUT):
    content = str(content)
    if len(content) <= limit:
        return content
    marker = f"\n...[bounded preview; omitted {len(content) - limit} chars]...\n"
    available = max(0, limit - len(marker))
    head = available * 3 // 5
    return content[:head] + marker + content[-(available - head) :]


class ToolExecutor:
    STAGES = ("registry", "surface", "schema", "policy", "approval")

    def __init__(self, agent):
        self.agent = agent
        self._outcomes_by_state = {}
        self.recovery_policy = RecoveryPolicy()

    @staticmethod
    def _memory_snapshot(agent):
        """Fingerprint durable memory separately from the source workspace."""
        root = agent.project_memory.root
        snapshot = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                logical = path.relative_to(agent.root).as_posix()
                snapshot[logical] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:  # noqa: BLE001, S112 - files can move during inspection
                continue
        return snapshot

    @staticmethod
    def _snapshot_fingerprint(snapshot):
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _repeat_key(run_id, call_fingerprint, workspace_fingerprint, memory_snapshot):
        return (
            str(run_id),
            str(call_fingerprint),
            str(workspace_fingerprint),
            tuple(sorted(memory_snapshot.items())),
        )

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

        tool = agent.all_tools.get(name)
        if tool is None:
            return self._rejected(call, fingerprint, admission, "unknown_tool", "registry", "unknown tool", started)
        admission["stages"].append({"stage": "registry", "status": "passed"})

        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return self._rejected(call, fingerprint, admission, "tool_not_allowed", "surface", "tool outside run surface", started)
        admission["stages"].append({"stage": "surface", "status": "passed"})

        try:
            args = agent.validate_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - admission converts validator failures to outcomes
            detail = str(exc)
            return self._rejected(
                call, fingerprint, admission, "invalid_arguments", "schema", detail, started,
                security_event_type="path_escape" if "path escapes workspace" in detail else "",
            )
        call = ToolCall(name, args, call.call_id)
        fingerprint = canonical_fingerprint(name, args)
        admission["stages"].append({"stage": "schema", "status": "passed"})

        agent.refresh_prefix()
        workspace_state_before = agent.capture_workspace_snapshot()
        workspace_before = self._snapshot_fingerprint(workspace_state_before)
        memory_before = self._memory_snapshot(agent) if name in {"memory_store", "memory_forget"} else {}
        run_id = agent.current_task_state.run_id if agent.current_task_state else "manual"
        repeat_key = self._repeat_key(run_id, fingerprint, workspace_before, memory_before)
        repeat_reason = self._repeat_block_reason(self._outcomes_by_state.get(repeat_key, ()))
        if repeat_reason:
            return self._rejected(
                call, fingerprint, admission, "repeated_identical_call", "policy",
                repeat_reason, started,
            )
        admission["stages"].append({"stage": "policy", "status": "passed"})

        if tool["risky"] and not agent.approve(name, args):
            return self._rejected(
                call, fingerprint, admission, "approval_denied", "approval", "approval denied", started,
                security_event_type="read_only_block" if agent.read_only else "approval_denied",
            )
        admission["stages"].append({"stage": "approval", "status": "passed"})
        admission["status"] = "admitted"

        before = workspace_state_before if tool["risky"] else {}
        agent.emit_event(
            agent.current_task_state,
            "operation_started",
            {
                "tool_call_id": call.call_id,
                "tool_name": name,
                "call_fingerprint": fingerprint,
                "risky": bool(tool["risky"]),
            },
            correlation_id=call.call_id,
        )
        try:
            artifact_content = str(tool["run"](args))
            content = bounded_tool_output(artifact_content)
            after = agent.capture_workspace_snapshot() if tool["risky"] else before
            paths, diff = agent.diff_workspace_snapshots(before, after)
            memory_after = (
                self._memory_snapshot(agent)
                if name in {"memory_store", "memory_forget"}
                else memory_before
            )
            memory_paths, memory_diff = agent.diff_workspace_snapshots(memory_before, memory_after)
            effect_scope = (
                "project_memory"
                if memory_paths and not paths
                else ("mixed" if memory_paths else "workspace")
            )
            paths = [*paths, *memory_paths]
            diff = [*diff, *memory_diff]
            if paths and effect_scope in {"workspace", "mixed"}:
                agent.refresh_prefix(force=True)
            status, failure = self._classify_result(name, content, bool(paths))
            side_effect = "partial" if status == "partial_success" else ("changed" if paths else "none")
            outcome = self._outcome(
                call, fingerprint, admission, status,
                "completed" if status in {"ok", "partial_success"} else "failed",
                side_effect, content, started, failure=failure, affected_paths=paths,
                diff_summary=diff, risky=tool["risky"], effect_scope=effect_scope,
                artifact_content=artifact_content,
            )
            agent.update_memory_after_tool(name, args, content, outcome)
        except Exception as exc:  # noqa: BLE001 - tool boundary must capture arbitrary runner failures
            after = agent.capture_workspace_snapshot() if tool["risky"] else before
            paths, diff = agent.diff_workspace_snapshots(before, after)
            memory_after = (
                self._memory_snapshot(agent)
                if name in {"memory_store", "memory_forget"}
                else memory_before
            )
            memory_paths, memory_diff = agent.diff_workspace_snapshots(memory_before, memory_after)
            effect_scope = (
                "project_memory"
                if memory_paths and not paths
                else ("mixed" if memory_paths else "workspace")
            )
            paths = [*paths, *memory_paths]
            diff = [*diff, *memory_diff]
            if paths and effect_scope in {"workspace", "mixed"}:
                agent.refresh_prefix(force=True)
            partial = bool(paths)
            outcome = self._outcome(
                call, fingerprint, admission, "partial_success" if partial else "error", "failed",
                "partial" if partial else "none", f"error: tool {name} failed: {exc}", started,
                failure=FailureInfo("tool_partial_success" if partial else "tool_failed", "execution", str(exc), True),
                affected_paths=paths, diff_summary=diff, risky=tool["risky"],
                effect_scope=effect_scope,
                security_event_type="path_escape" if "path escapes workspace" in str(exc) else "",
            )

        content_fingerprint = agent.content_workspace_fingerprint()
        result_key = self._repeat_key(run_id, fingerprint, content_fingerprint, memory_after)
        self._outcomes_by_state.setdefault(result_key, []).append(outcome)
        agent.evidence_ledger.record_tool(outcome, content_fingerprint)
        agent.emit_event(
            agent.current_task_state,
            "operation_finished",
            {
                "tool_call_id": call.call_id,
                "tool_name": name,
                "content_workspace_fingerprint": content_fingerprint,
                "outcome": outcome.to_dict(),
            },
            correlation_id=call.call_id,
        )
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

    def _rejected(self, call, fingerprint, admission, code, stage, detail, started, security_event_type=""):
        admission["stages"].append({"stage": stage, "status": "rejected", "code": code})
        admission["status"] = "rejected"
        outcome = self._outcome(
            call, fingerprint, admission, "rejected", "not_started", "none",
            f"error: {detail} for {call.name}", started,
            failure=FailureInfo(code, "admission", detail, code == "repeated_identical_call"),
            risky=True, security_event_type=security_event_type,
        )
        self.agent.emit_event(
            self.agent.current_task_state,
            "tool_rejected",
            {"tool_call_id": call.call_id, "tool_name": call.name, "outcome": outcome.to_dict()},
            correlation_id=call.call_id,
        )
        return outcome

    def _outcome(
        self, call, fingerprint, admission, status, execution_state, side_effect_state,
        content, started, *, failure=None, affected_paths=(), diff_summary=(), risky=False,
        security_event_type="", effect_scope="workspace", artifact_content=None,
    ):
        run_id = self.agent.current_task_state.run_id if self.agent.current_task_state else "manual"
        descriptor = self.agent.artifact_store.write_tool_output(
            run_id,
            call.call_id,
            call.name,
            content if artifact_content is None else artifact_content,
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
            workspace_fingerprint=self.agent.workspace.fingerprint(),
            duration_ms=duration_ms,
            artifact_id=descriptor["artifact_id"],
            artifact=descriptor,
            metadata={
                "security_event_type": security_event_type,
                "risk_level": "high" if risky else "low",
                "read_only": not risky,
                "effect_scope": effect_scope if side_effect_state != "none" else "none",
            },
        )
