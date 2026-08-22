"""Runtime-owned checks performed when the model requests completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .verification import changed_python_syntax_issues

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class CompletionResult:
    final_answer: str | None = None
    status: str = ""
    instruction: str = ""

    @property
    def allowed(self):
        return self.final_answer is not None


class CompletionController:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def assess(self, final: str) -> CompletionResult:
        blocker = self._static_blocker()
        if blocker:
            status, instruction = blocker
            return CompletionResult(
                status=status,
                instruction=instruction,
            )

        fingerprint, verification_guidance = self._ensure_verification()
        if verification_guidance:
            return CompletionResult(
                status="verification_failed",
                instruction=verification_guidance,
            )

        decision = self.runtime.run.evidence.assess_completion(fingerprint)
        if not decision.allowed:
            return CompletionResult(
                status=decision.status,
                instruction=(
                    f"Runtime completion gate: {decision.reason}. "
                    "Inspect or repair before returning a final answer."
                ),
            )
        return CompletionResult(final_answer=final)

    def _static_blocker(self):
        runtime = self.runtime
        subtask_issue = (
            runtime.dependencies.subagents.completion_issue()
            if runtime.dependencies.subagents is not None
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

    def _ensure_verification(self):
        runtime = self.runtime
        unresolved = runtime.run.evidence.unresolved_effects()
        workspace_unresolved = any(
            effect.get("effect_scope") in {"workspace", "mixed"}
            for effect in unresolved
        )
        needs_verification = bool(
            (runtime.run.evidence.changed_paths or workspace_unresolved)
            and runtime.config.verification_command
        )
        if not needs_verification:
            return "", ""
        fingerprint = runtime.workspace.content_fingerprint(force=True)
        verification = runtime.run.evidence.current_verification(fingerprint)
        if verification is None:
            verification = runtime.run_verification(fingerprint)
            runtime.emit_event(
                "verification_result",
                verification or {"status": "skipped"},
            )
        if not verification or verification.get("status") != "passed":
            return fingerprint, (
                "Runtime verification failed; inspect and repair before "
                "submit_final.\n"
                + str((verification or {}).get("output", "verification unavailable"))
            )
        return fingerprint, ""
