"""Task-scoped effects and verifier evidence projected from a Run Journal."""

from dataclasses import dataclass, field

OBSERVATION_TOOLS = frozenset({"read_file", "list_files", "search"})


def fact_from_entry(entry):
    payload = dict(entry.payload)
    outcome = dict(payload.get("outcome", {}) or {})
    return {
        "tool_call_id": str(outcome.get("tool_call_id", "")),
        "tool": str(outcome.get("tool_name", "")),
        "status": str(outcome.get("status", "error")),
        "side_effect_state": str(outcome.get("side_effect_state", "unknown")),
        "affected_paths": list(outcome.get("affected_paths", [])),
        "workspace_revision": int(payload.get("workspace_revision", 0)),
        "artifact_id": str(outcome.get("artifact_id", "")),
        "effect_scope": str(
            dict(outcome.get("metadata", {}) or {}).get("effect_scope", "none")
        ),
    }


@dataclass
class EvidenceLedger:
    observations: list[dict] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)
    verifications: list[dict] = field(default_factory=list)

    @classmethod
    def from_entries(cls, entries):
        ledger = cls()
        for entry in entries:
            ledger.apply_entry(entry)
        return ledger

    def apply_entry(self, entry):
        if entry.kind == "verification_result" and entry.payload.get(
            "verification_id"
        ):
            self.verifications.append(dict(entry.payload))
            return
        if entry.kind != "tool_result":
            return
        fact = fact_from_entry(entry)
        if fact["tool"] in OBSERVATION_TOOLS and fact["status"] == "ok":
            self.observations.append(fact)
        if fact["side_effect_state"] != "none":
            self.effects.append(fact)
            if fact["effect_scope"] in {"workspace", "mixed"}:
                for record in self.verifications:
                    if record.get("freshness") == "current":
                        record["freshness"] = "stale"
                        record["invalidated_by"] = fact["tool_call_id"]

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
    def control_changed_paths(self):
        return sorted(
            {
                path
                for item in self.effects
                if item.get("effect_scope") in {"project_memory", "mixed"}
                for path in item["affected_paths"]
                if path.startswith(".pico/")
            }
        )

    def current_verification(self, workspace_fingerprint):
        return next(
            (
                item
                for item in reversed(self.verifications)
                if item.get("status") == "passed"
                and item.get("freshness") == "current"
                and item.get("workspace_fingerprint")
                == str(workspace_fingerprint)
            ),
            None,
        )

    def to_dict(self):
        return {
            "observations": list(self.observations),
            "effects": list(self.effects),
            "verifications": list(self.verifications),
        }
