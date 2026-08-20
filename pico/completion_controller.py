"""Runtime-owned checks performed when the model requests completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .run_lifecycle import LoopFrame
from .verification import changed_python_syntax_issues

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class CompletionAssessment:
    final: str | None = None
    status: str = ""
    reason: str = ""
    guidance: str = ""

    @property
    def allowed(self):
        return self.final is not None


class CompletionController:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def assess(self, frame: LoopFrame, final: str) -> CompletionAssessment:
        blocker = self._static_blocker()
        if blocker:
            status, guidance = blocker
            return CompletionAssessment(
                status=status,
                reason=guidance,
                guidance=guidance,
            )

        verification_guidance = self._ensure_verification(frame)
        if verification_guidance:
            return CompletionAssessment(
                status="verification_failed",
                reason=verification_guidance,
                guidance=verification_guidance,
            )

        decision = frame.completion_gate.assess()
        if not decision.allowed:
            return CompletionAssessment(
                status=decision.status,
                reason=decision.reason,
                guidance=(
                    f"Runtime completion gate: {decision.reason}. "
                    "Inspect or repair before returning a final answer."
                ),
            )
        return CompletionAssessment(final=final)

    def _static_blocker(self):
        runtime = self.runtime
        subtask_issue = (
            runtime.services.subagents.completion_issue()
            if runtime.services.subagents is not None
            else ""
        )
        if subtask_issue:
            return "subtasks_incomplete", f"Runtime completion gate: {subtask_issue}."
        syntax_issues = changed_python_syntax_issues(runtime)
        if syntax_issues:
            return "syntax_invalid", (
                "Runtime completion gate: changed Python is invalid: "
                + "; ".join(syntax_issues)
            )
        return None

    def _ensure_verification(self, frame):
        runtime = self.runtime
        preliminary = frame.completion_gate.assess()
        needs_verification = bool(
            (runtime.run.evidence.changed_paths or not preliminary.allowed)
            and runtime.config.verification_command
        )
        if not needs_verification:
            return ""
        fingerprint = runtime.workspace.content_fingerprint(force=True)
        verification = runtime.run.evidence.current_verification(fingerprint)
        if verification is None:
            runtime.emit_event(
                frame.task_state,
                "verification_started",
                {"command": runtime.config.verification_command},
            )
            verification = runtime.run_verification(fingerprint)
            event = runtime.emit_event(
                frame.task_state,
                "verification_result",
                verification or {"status": "skipped"},
            )
            runtime.run.evidence.apply_entry(event)
        if not verification or verification.get("status") != "passed":
            return (
                "Runtime verification failed; inspect and repair before "
                "submit_final.\n"
                + str((verification or {}).get("output", "verification unavailable"))
            )
        frame.completion_gate.observe_verification(True)
        return ""
