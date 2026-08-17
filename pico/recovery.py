"""Deterministic correction policy derived from trusted tool facts."""

from .contracts import RecoveryAssessment


class RecoveryPolicy:
    def __init__(self):
        self.occurrences = {}

    def assess(self, failure, *, status, fingerprint, scope=""):
        signature = f"{scope}:{failure.code}:{fingerprint}"
        occurrence = self.occurrences.get(signature, 0) + 1
        self.occurrences[signature] = occurrence

        if failure.code == "approval_denied":
            return RecoveryAssessment(
                "stop", "tool authority was not granted", "user_authority_required", occurrence,
                ("Ask the user for authority or choose a read-only action.",),
            )
        if failure.code == "repeated_identical_call":
            return RecoveryAssessment(
                "replan", "the call made no progress", "retry_after_change", occurrence,
                ("Change the arguments, gather new evidence, or finish the task.",),
            )
        if status == "partial_success":
            return RecoveryAssessment(
                "repair", "execution failed after changing the workspace", "retry_after_change", occurrence,
                ("Inspect affected paths before retrying.", "Repair or verify the partial change."),
            )
        if failure.category == "admission":
            return RecoveryAssessment(
                "replan", "the requested action was not admitted", "retry_after_change", occurrence,
                ("Correct the call using the admission failure detail.",),
            )
        return RecoveryAssessment(
            "retry" if occurrence == 1 and failure.retryable else "replan",
            "tool execution failed",
            "retry_after_change" if failure.retryable else "nonrecoverable",
            occurrence,
            ("Use the failure detail and workspace evidence before the next action.",),
        )
