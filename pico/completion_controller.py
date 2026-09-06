"""Runtime-owned completion checks derived from TaskContract and RunEvidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .evidence import verification_is_current
from .verification import capture_changed_path_states

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class CompletionDecision:
    status: str
    instruction: str
    evidence: str = ""

    def __post_init__(self):
        if not self.status.strip() or not self.instruction.strip():
            raise ValueError("completion decision requires status and content")

    @property
    def allowed(self):
        return self.status == "allowed"


class CompletionController:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def assess(self, final: str) -> CompletionDecision:
        blocker = (
            self._static_blocker()
            or self._effect_blocker()
            or self._task_requirement_blocker()
            or self._workspace_drift_blocker()
        )
        if blocker:
            return CompletionDecision(*blocker)

        guidance = self._ensure_verification()
        if guidance:
            instruction, evidence = guidance
            return CompletionDecision("verification_failed", instruction, evidence)
        blocker = self._effect_blocker()
        if blocker:
            return CompletionDecision(*blocker)
        return CompletionDecision("allowed", final)

    def _effect_blocker(self):
        effects = self.runtime.run.evidence.unverifiable_effects()
        if not effects:
            return None
        paths = sorted(
            {
                path
                for effect in effects
                for path in effect.get("affected_paths", ())
            }
        )
        detail = ", ".join(paths) or "unknown workspace state"
        return (
            "partial",
            (
                "Resolve the unknown or untracked workspace side effects before "
                "submitting completion."
            ),
            detail,
        )

    def _workspace_drift_blocker(self):
        drift = self.runtime.run.evidence.change_set.workspace_drift(
            self.runtime.workspace.root
        )
        if not drift:
            return None
        paths = ", ".join(item["path"] for item in drift)
        return (
            "workspace_drift",
            (
                "Restore the Runtime-projected workspace state or reset the Run "
                "before submitting completion."
            ),
            paths,
        )

    def _task_requirement_blocker(self):
        task = self.runtime.run.projection
        if task.contract is None:
            return (
                "task_requirements_missing",
                "Task requirements are unavailable; stop and restore the Run state.",
                "",
            )
        evidence = self.runtime.run.evidence
        contract = task.contract
        if not contract.allows_workspace_mutation and evidence.touched_paths:
            return (
                "ask_mode_violation",
                "Ask mode produced workspace changes; restore them or reset the Run.",
                ", ".join(evidence.touched_paths),
            )
        return None

    def _static_blocker(self):
        issue = self.runtime.run.projection.children.completion_issue()
        if issue:
            return (
                "subtasks_incomplete",
                "Resolve incomplete Child work before submitting completion.",
                issue,
            )
        return None

    def _ensure_verification(self):
        runtime = self.runtime
        task = runtime.run.projection
        required = bool(
            task.contract is not None
            and task.contract.verify_changes
            and runtime.run.evidence.has_net_workspace_change
        )
        # A failed tool is historical fact. Verify its current tracked effects,
        # including changes later reverted, without requiring another mutation.
        if not (required or runtime.run.evidence.partial_workspace_effects()):
            return None
        if not runtime.config.verification_command:
            return (
                (
                    "Configure the required Runtime verification command before "
                    "submitting completion."
                ),
                "",
            )

        sequence = runtime.run.evidence.last_workspace_mutation_sequence
        # Changed-path revisions cannot establish whether dependencies or the
        # execution environment changed. Each submission runs its own verifier.
        current = runtime.run_verification(sequence)
        if current is not None:
            runtime.emit_event("verification_result", current)
        if current and current.get("status") == "infrastructure_error":
            raise RuntimeError(
                "Runtime verification infrastructure error: "
                + str(current.get("output") or "verification unavailable")
            )
        states = capture_changed_path_states(
            runtime.workspace.root, runtime.run.evidence.changed_paths,
        )
        if current is None or not verification_is_current(
            current, runtime.run.evidence.last_workspace_mutation_sequence,
            states, runtime.config.verification_command,
        ):
            return (
                (
                    "The workspace changed during Runtime verification; submit "
                    "again to run verification against the current state."
                ),
                "",
            )
        if current.get("status") != "passed":
            return (
                (
                    "Runtime verification failed; inspect the untrusted evidence, "
                    "repair the code, and submit again."
                ),
                str(current.get("output") or "verification unavailable"),
            )
        return None
