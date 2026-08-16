"""Terminal-state guard for truthful completion claims."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CompletionDecision:
    allowed: bool
    status: str
    reason: str = ""


class CompletionGate:
    """Prevent a clean success after unresolved partial side effects."""

    def __init__(self):
        self.outcomes = []
        self.restored_partial_paths = set()
        self.verification_passed = False

    def restore_partial_paths(self, paths):
        self.restored_partial_paths.update(str(path) for path in paths if str(path))

    def observe(self, outcome):
        self.outcomes.append(outcome)

    def observe_verification(self, passed):
        self.verification_passed = bool(passed)

    def assess(self):
        unresolved = []
        for index, outcome in enumerate(self.outcomes):
            if outcome.status != "partial_success" and outcome.side_effect_state != "unknown":
                continue
            affected = set(outcome.affected_paths)
            repaired = any(
                later.status == "ok"
                and later.side_effect_state == "changed"
                and (not affected or affected.issubset(set(later.affected_paths)))
                for later in self.outcomes[index + 1 :]
            )
            if not repaired and not self.verification_passed:
                unresolved.append(outcome)
        repaired_paths = {
            path
            for outcome in self.outcomes
            if outcome.status == "ok" and outcome.side_effect_state == "changed"
            for path in outcome.affected_paths
        }
        restored_unresolved = (
            set() if self.verification_passed else self.restored_partial_paths - repaired_paths
        )
        if unresolved or restored_unresolved:
            paths = sorted({path for outcome in unresolved for path in outcome.affected_paths} | restored_unresolved)
            detail = ", ".join(paths) or "unknown workspace state"
            return CompletionDecision(False, "partial", f"unresolved partial side effects: {detail}")
        return CompletionDecision(True, "completed")
