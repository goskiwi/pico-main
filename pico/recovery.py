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
                "stop",
                occurrence,
                ("Ask the user for authority or choose a read-only action.",),
            )
        if failure.code == "repeated_identical_call":
            return RecoveryAssessment(
                "replan",
                occurrence,
                ("Change the arguments, gather new evidence, or finish the task.",),
            )
        if status == "partial_success":
            return RecoveryAssessment(
                "repair",
                occurrence,
                ("Inspect affected paths before retrying.", "Repair or verify the partial change."),
            )
        if failure.category == "admission":
            return RecoveryAssessment(
                "replan",
                occurrence,
                ("Correct the call using the admission failure detail.",),
            )
        return RecoveryAssessment(
            "retry" if occurrence == 1 and failure.retryable else "replan",
            occurrence,
            ("Use the failure detail and workspace evidence before the next action.",),
        )
