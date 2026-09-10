"""Final acceptance uses the same verification service as the verify tool."""

from dataclasses import replace

from .verification_service import VerificationResult, VerificationService


class CompletionController:
    def __init__(self, runtime):
        self.runtime = runtime
        self.verifier = VerificationService(runtime)

    def check(self, proposed_answer, execution):
        blocked = self.verifier.inspect(execution)
        if blocked:
            return blocked
        session = self.runtime.session
        if not self.runtime.config.verification_command.strip():
            if session.task_policy.get("verification_floor"):
                return VerificationResult("stop", "Required acceptance command is unavailable.",
                                          "verification_required")
            detail = proposed_answer
            if session.verification_required:
                detail += "\n\nRuntime: no independent acceptance command was configured; "
                detail += "completion does not certify independent acceptance."
            return VerificationResult("success", detail)
        result = self.verifier.run(execution)
        if result.allowed:
            result = replace(result, detail=proposed_answer)
        return result
