"""Task-scoped observations, effects, and verifier evidence."""

from dataclasses import dataclass, field

OBSERVATION_TOOLS = frozenset({"read_file", "list_files", "search", "query_repo_map"})


def fact_from_outcome(outcome, workspace_fingerprint):
    return {
        "tool_call_id": outcome.tool_call_id,
        "tool": outcome.tool_name,
        "status": outcome.status,
        "side_effect_state": outcome.side_effect_state,
        "affected_paths": list(outcome.affected_paths),
        "workspace_fingerprint": str(workspace_fingerprint),
        "artifact_id": outcome.artifact_id,
        "effect_scope": str(outcome.metadata.get("effect_scope", "none")),
    }


def fact_from_event(event):
    payload = dict(event.get("payload", {}))
    outcome = dict(payload.get("outcome", {}) or {})
    return {
        "tool_call_id": str(outcome.get("tool_call_id", "")),
        "tool": str(outcome.get("tool_name", "")),
        "status": str(outcome.get("status", "error")),
        "side_effect_state": str(outcome.get("side_effect_state", "unknown")),
        "affected_paths": list(outcome.get("affected_paths", [])),
        "workspace_fingerprint": str(payload.get("content_workspace_fingerprint", "")),
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
    def from_events(cls, events):
        ledger = cls()
        for event in events:
            event_type = event.get("event_type")
            payload = dict(event.get("payload", {}))
            if event_type == "operation_finished":
                ledger.apply_fact(fact_from_event(event))
            elif event_type == "verification_finished" and payload.get("verification_id"):
                ledger.record_verification(payload)
        return ledger

    def record_tool(self, outcome, workspace_fingerprint):
        self.apply_fact(fact_from_outcome(outcome, workspace_fingerprint))

    def apply_fact(self, fact):
        fact = dict(fact)
        if fact["tool"] in OBSERVATION_TOOLS and fact["status"] == "ok":
            self.observations.append(fact)
        if fact["side_effect_state"] != "none":
            self.effects.append(fact)
            if fact["effect_scope"] in {"workspace", "mixed"}:
                for record in self.verifications:
                    if record.get("freshness") == "current":
                        record["freshness"] = "stale"
                        record["invalidated_by"] = fact["tool_call_id"]

    def record_verification(self, record):
        self.verifications.append(dict(record))

    @property
    def changed_paths(self):
        return sorted({
            path
            for item in self.effects
            if item.get("effect_scope") in {"workspace", "mixed"}
            for path in item["affected_paths"]
            if not path.startswith(".pico/")
        })

    @property
    def control_changed_paths(self):
        return sorted({
            path
            for item in self.effects
            if item.get("effect_scope") in {"project_memory", "mixed"}
            for path in item["affected_paths"]
            if path.startswith(".pico/")
        })

    @property
    def has_uncertain_effects(self):
        return any(item["side_effect_state"] in {"partial", "unknown"} for item in self.effects)

    def current_verification(self, workspace_fingerprint):
        return next((item for item in reversed(self.verifications)
                     if item.get("status") == "passed" and item.get("freshness") == "current"
                     and item.get("workspace_fingerprint") == str(workspace_fingerprint)), None)

    def to_dict(self):
        return {"observations": list(self.observations), "effects": list(self.effects),
                "verifications": list(self.verifications)}
