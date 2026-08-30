"""Runtime-owned completion checks derived from TaskContract and RunEvidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .verification import capture_changed_path_states

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
        blocker = (
            self._static_blocker()
            or self._task_requirement_blocker()
            or self._workspace_drift_blocker()
        )
        if blocker:
            status, instruction = blocker
            return CompletionResult(status=status, instruction=instruction)

        mutation_sequence, path_states, guidance = self._ensure_verification()
        if guidance:
            return CompletionResult(
                status="verification_failed",
                instruction=guidance,
            )
        evidence_check = self.runtime.run.evidence.assess_completion(
            mutation_sequence,
            path_states,
        )
        if not evidence_check.allowed:
            return CompletionResult(
                status=evidence_check.status,
                instruction=(
                    f"Runtime completion gate: {evidence_check.reason}. "
                    "Inspect or repair before returning a final answer."
                ),
            )
        return CompletionResult(final_answer=final)

    def _workspace_drift_blocker(self):
        drift = self.runtime.run.evidence.change_set.workspace_drift(
            self.runtime.workspace.root
        )
        if not drift:
            return None
        paths = ", ".join(item["path"] for item in drift)
        return "workspace_drift", (
            "Runtime completion gate: tracked workspace paths changed outside this "
            f"Run: {paths}. Restore the projected state or reset the Run before "
            "submitting a final answer."
        )

    def _task_requirement_blocker(self):
        task = self.runtime.run.task
        if task is None:
            return "task_requirements_missing", (
                "Runtime completion gate: task requirements are unavailable."
            )
        evidence = self.runtime.run.evidence
        contract = task.contract
        if contract.task_kind == "read_only":
            if evidence.touched_paths:
                return "read_only_violation", (
                    "Runtime completion gate: read-only task produced workspace changes."
                )
            if evidence.successful_observation_count < 1:
                return "observation_required", (
                    "Runtime completion gate: read-only task requires at least one "
                    "successful observation tool result."
                )
        if (
            contract.task_kind == "modify"
            and contract.requires_workspace_change
            and not evidence.has_net_workspace_change
        ):
            return "workspace_change_required", (
                "Runtime completion gate: this task requires an actual net workspace "
                "change."
            )
        return None

    def _static_blocker(self):
        subagents = self.runtime.dependencies.subagents
        issue = subagents.completion_issue() if subagents is not None else ""
        if issue:
            return "subtasks_incomplete", f"Runtime completion gate: {issue}."
        return None

    def _ensure_verification(self):
        runtime = self.runtime
        task = runtime.run.task
        required = bool(task and task.contract.requires_verification)
        partial_repair_requires_verification = bool(
            runtime.run.evidence.repaired_partials_requiring_verification()
        )
        if not (required or partial_repair_requires_verification):
            return None, None, ""
        if not runtime.config.verification_command:
            return None, None, (
                "Runtime verification is required, but no verification command "
                "is configured."
            )

        sequence = runtime.run.evidence.last_workspace_mutation_sequence
        states = capture_changed_path_states(
            runtime.workspace.root,
            runtime.run.evidence.changed_paths,
        )
        current = runtime.run.evidence.latest_verification_for_state(
            sequence,
            states,
        )
        # Only a passing result is reusable. A failed or infrastructure result
        # must be retryable on the same code state after repairs to the environment.
        if current is None or current.get("status") != "passed":
            payload = runtime.run_verification(sequence)
            if payload is not None:
                runtime.emit_event("verification_result", payload)

        states = capture_changed_path_states(
            runtime.workspace.root,
            runtime.run.evidence.changed_paths,
        )
        current = runtime.run.evidence.latest_verification_for_state(
            sequence,
            states,
        )
        if current and current.get("status") == "infrastructure_error":
            raise RuntimeError(
                "Runtime verification infrastructure error: "
                + str(current.get("output") or "verification unavailable")
            )
        if current is None:
            return sequence, states, (
                "Runtime workspace changed during verification; run verification "
                "again before submit_final."
            )
        if current.get("status") != "passed":
            return sequence, states, (
                "Runtime verification failed; inspect and repair before "
                "submit_final.\n"
                + str(current.get("output") or "verification unavailable")
            )
        return sequence, states, ""
