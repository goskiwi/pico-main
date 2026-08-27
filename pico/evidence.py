"""Task-scoped effects and verifier evidence projected from a Run Log."""

from dataclasses import dataclass, field


def fact_from_event(entry):
    outcome = dict(entry.payload.get("outcome", {}) or {})
    return {
        "tool_call_id": str(outcome.get("tool_call_id", "")),
        "tool": str(outcome.get("tool_name", "")),
        "status": str(outcome.get("status", "error")),
        "side_effect_state": str(outcome.get("side_effect_state", "unknown")),
        "affected_paths": list(outcome.get("affected_paths", [])),
        "effect_scope": str(outcome.get("effect_scope", "none")),
        "event_sequence": int(entry.sequence),
    }


@dataclass
class RunEvidence:
    effects: list[dict] = field(default_factory=list)
    verifications: list[dict] = field(default_factory=list)

    @classmethod
    def from_events(cls, events):
        evidence = cls()
        for entry in events:
            evidence.apply_event(entry)
        return evidence

    def apply_event(self, entry):
        if entry.kind == "verification_result":
            self.verifications.append(dict(entry.payload))
            return
        if entry.kind != "tool_result":
            return
        fact = fact_from_event(entry)
        if fact["side_effect_state"] != "none":
            self.effects.append(fact)
            if fact["effect_scope"] in {"workspace", "mixed"}:
                for record in self.verifications:
                    if record.get("freshness") == "current":
                        record["freshness"] = "stale"

    @property
    def changed_paths(self):
        return sorted(
            {
                path
                for item in self.effects
                if item.get("effect_scope") in {"workspace", "mixed"}
                for path in item["affected_paths"]
                if not path.startswith(".pico/")
            }
        )

    @property
    def last_workspace_mutation_sequence(self):
        return max(
            (
                int(item.get("event_sequence", 0))
                for item in self.effects
                if item.get("effect_scope") in {"workspace", "mixed"}
            ),
            default=0,
        )

    def current_verification(
        self,
        expected_workspace_mutation_sequence,
        current_changed_path_states,
    ):
        return next(
            (
                item
                for item in reversed(self.verifications)
                if item.get("status") == "passed"
                and item.get("freshness") == "current"
                and int(item.get("finished_workspace_mutation_sequence", -1))
                == int(expected_workspace_mutation_sequence)
                and item.get("finished_changed_path_states")
                == dict(current_changed_path_states)
            ),
            None,
        )

    def unresolved_effects(self):
        unresolved = []
        for index, effect in enumerate(self.effects):
            if effect.get("side_effect_state") not in {"partial", "unknown"}:
                continue
            affected = set(effect.get("affected_paths", ()))
            effect_scope = effect.get("effect_scope", "none")
            repaired = bool(affected) and any(
                later.get("status") == "success"
                and later.get("side_effect_state") == "changed"
                and later.get("effect_scope") in {effect_scope, "mixed"}
                and affected.issubset(set(later.get("affected_paths", ())))
                for later in self.effects[index + 1 :]
            )
            if not repaired:
                unresolved.append(effect)
        return unresolved

    def assess_completion(
        self,
        expected_workspace_mutation_sequence=None,
        current_changed_path_states=None,
    ):
        unresolved = self.unresolved_effects()
        verified = bool(
            expected_workspace_mutation_sequence is not None
            and current_changed_path_states is not None
            and self.current_verification(
                expected_workspace_mutation_sequence,
                current_changed_path_states,
            )
            is not None
        )
        remaining = [
            effect
            for effect in unresolved
            if not (
                verified
                and effect.get("effect_scope") in {"workspace", "mixed"}
            )
        ]
        if remaining:
            paths = sorted(
                {
                    path
                    for effect in remaining
                    for path in effect.get("affected_paths", ())
                }
            )
            detail = ", ".join(paths) or "unknown workspace state"
            return EvidenceCompletionCheck(
                False,
                "partial",
                f"unresolved partial side effects: {detail}",
            )
        return EvidenceCompletionCheck(True, "completed")

@dataclass(frozen=True)
class EvidenceCompletionCheck:
    allowed: bool
    status: str
    reason: str = ""
