"""Runtime-owned completion checks derived from TaskContract and RunEvidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .verification import (
    ResolvedVerificationPolicy,
    capture_changed_path_states,
)

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

    @property
    def verification_required(self):
        return self.status == "verification_required"


class CompletionController:
    def __init__(self, runtime: Pico):
        self.runtime = runtime

    def resolve_verification_policy(self):
        contract = self.runtime.run.projection.contract
        if contract is None:
            raise RuntimeError("verification policy requires an active TaskContract")
        return ResolvedVerificationPolicy.resolve(
            contract,
            self.runtime.config.verification_command,
        )

    def assess(
        self,
        final: str,
        policy: ResolvedVerificationPolicy,
    ) -> CompletionDecision:
        blocker = (
            self._effect_blocker()
            or self._task_requirement_blocker()
            or self._workspace_drift_blocker()
        )
        if blocker:
            return CompletionDecision(*blocker)
        if not self._verification_required(policy):
            return CompletionDecision("allowed", final)
        if not policy.command:
            return CompletionDecision(
                "verification_failed",
                "Configure the required Runtime verification command before "
                "submitting completion.",
            )
        return CompletionDecision(
            "verification_required",
            "Run the configured Runtime verification before completion.",
        )

    def assess_verification(
        self,
        final: str,
        policy: ResolvedVerificationPolicy,
    ) -> CompletionDecision:
        """Assess the latest persisted verification without executing one."""

        blocker = (
            self._static_blocker()
            or self._effect_blocker()
            or self._task_requirement_blocker()
            or self._workspace_drift_blocker()
        )
        if blocker:
            return CompletionDecision(*blocker)
        if not self._verification_required(policy):
            return CompletionDecision("allowed", final)
        runtime = self.runtime
        states = capture_changed_path_states(
            runtime.workspace.root,
            runtime.run.evidence.changed_paths,
            execution_context=runtime.run.execution_context,
        )
        current = runtime.run.evidence.latest_verification_for_state(
            runtime.run.evidence.last_workspace_mutation_sequence,
            states,
            policy.command,
        )
        if current is None:
            return CompletionDecision(
                "verification_failed",
                "The workspace changed during Runtime verification; submit "
                "again to run verification against the current state.",
            )
        if current.get("status") == "infrastructure_error":
            raise RuntimeError(
                "Runtime verification infrastructure error: "
                + str(current.get("output") or "verification unavailable")
            )
        if current.get("status") != "passed":
            return CompletionDecision(
                "verification_failed",
                "Runtime verification failed; inspect the untrusted evidence, "
                "repair the code, and submit again.",
                str(current.get("output") or "verification unavailable"),
            )
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
        root = self.runtime.workspace.root
        drift = self.runtime.run.evidence.change_set.workspace_drift(root)
        if not drift:
            return None
        redirected = [
            item["path"] for item in drift
            if (root / item["path"]).resolve() != root / item["path"]
        ]
        if redirected:
            return (
                "workspace_drift",
                (
                    "Ask the user to restore the original file targets for these paths "
                    "while preserving external edits. Reading redirected paths cannot "
                    "acknowledge this drift."
                ),
                ", ".join(redirected),
            )
        paths = ", ".join(item["path"] for item in drift)
        return (
            "workspace_drift",
            (
                "Read the changed files with read_file to observe their current versions, "
                "preserve external edits, then continue and verify before submitting."
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
        if contract.write_scope.mode == "none" and evidence.touched_paths:
            return (
                "ask_mode_violation",
                "Ask mode produced workspace changes; restore them or reset the Run.",
                ", ".join(evidence.touched_paths),
            )
        return None

    def _verification_required(self, policy: ResolvedVerificationPolicy):
        runtime = self.runtime
        required = bool(
            policy.verify_net_changes
            and runtime.run.evidence.has_net_workspace_change
        )
        # A failed tool is historical fact. Verify its current tracked effects,
        # including changes later reverted, without requiring another mutation.
        return bool(required or runtime.run.evidence.partial_workspace_effects())
