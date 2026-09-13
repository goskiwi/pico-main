"""Runtime-owned safety checks before a model-declared completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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

    def evaluate(self, final):
        """Accept completion only when Runtime-owned safety state is settled."""

        blocker = (
            self._task_requirement_blocker()
            or self._workspace_drift_blocker()
        )
        if blocker:
            return CompletionDecision(*blocker)
        return CompletionDecision("allowed", final)

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
                "preserve external edits, then continue before submitting."
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
