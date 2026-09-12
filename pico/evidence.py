"""Current file changes, uncertain effects and latest verification, rebuilt from RunLog."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .mutations import ABSENT_REVISION, file_revision, unified_text_diff

WORKSPACE_SCOPES = frozenset({"workspace"})


class WorkspaceDriftError(RuntimeError):
    def __init__(self, drift):
        self.drift = tuple(dict(item) for item in drift)
        paths = ", ".join(item["path"] for item in self.drift)
        super().__init__(
            "workspace changed after the last Runtime transition: " + paths
        )


@dataclass
class FileChange:
    path: str
    first_before_state: str
    first_before_artifact_id: str
    current_after_state: str
    last_mutation_sequence: int
    external_change_observed: bool = False

    @property
    def net_changed(self):
        return self.first_before_state != self.current_after_state

    def apply(self, transition, sequence):
        if str(transition["path"]) != self.path:
            raise ValueError("path transition does not match Run change")
        if str(transition["before_state"]) != self.current_after_state:
            raise ValueError(f"workspace transition chain is discontinuous for {self.path}")
        self.current_after_state = str(transition["after_state"])
        self.last_mutation_sequence = int(sequence)

    def to_dict(self):
        return {
            "path": self.path,
            "first_before_state": self.first_before_state,
            "first_before_artifact_id": self.first_before_artifact_id,
            "current_after_state": self.current_after_state,
            "last_mutation_sequence": self.last_mutation_sequence,
            "net_changed": self.net_changed,
            "external_change_observed": self.external_change_observed,
        }

@dataclass
class RunChangeSet:
    files: dict[str, FileChange] = field(default_factory=dict)

    def apply_effect(self, effect):
        if effect["effect_scope"] not in WORKSPACE_SCOPES:
            return self
        if effect["side_effect_state"] not in {"changed", "partial"}:
            return self
        transitions = {
            str(item["path"]): item for item in effect.get("path_transitions", ())
        }
        missing = sorted(set(effect["affected_paths"]) - set(transitions))
        if missing:
            raise ValueError(
                "workspace effect lacks path transitions: " + ", ".join(missing)
            )
        for path in sorted(transitions):
            transition = transitions[path]
            current = self.files.get(path)
            if current is None:
                self.files[path] = FileChange(
                    path=path,
                    first_before_state=str(transition["before_state"]),
                    first_before_artifact_id=str(
                        transition.get("before_artifact_id", "")
                    ),
                    current_after_state=str(transition["after_state"]),
                    last_mutation_sequence=int(effect["event_sequence"]),
                )
            else:
                current.apply(transition, effect["event_sequence"])
        return self

    @property
    def touched_paths(self):
        return tuple(sorted(self.files))

    @property
    def net_changed_paths(self):
        return tuple(
            path for path in sorted(self.files) if self.files[path].net_changed
        )

    @property
    def current_net_path_states(self):
        return {
            path: self.files[path].current_after_state
            for path in self.net_changed_paths
        }

    def to_dict(self):
        return {
            "touched_paths": list(self.touched_paths),
            "net_changed_paths": list(self.net_changed_paths),
            "files": {
                path: self.files[path].to_dict() for path in sorted(self.files)
            },
        }

    def workspace_drift(self, root):
        root = Path(root).resolve()
        drift = []
        for relative in self.touched_paths:
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"Run change path escapes workspace: {relative}"
                ) from exc
            projected_state = self.files[relative].current_after_state
            actual_state = file_revision(target)
            if actual_state != projected_state:
                drift.append(
                    {
                        "path": relative,
                        "projected_state": projected_state,
                        "actual_state": actual_state,
                    }
                )
        return tuple(drift)

    def require_current_workspace(self, root):
        drift = self.workspace_drift(root)
        if drift:
            raise WorkspaceDriftError(drift)
        return self

    def render_final_diff(self, root, artifact_store, run_id):
        root = Path(root).resolve()
        self.require_current_workspace(root)
        rendered = []
        for relative in self.net_changed_paths:
            change = self.files[relative]
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"Run change path escapes workspace: {relative}"
                ) from exc
            current_state = file_revision(target)
            before_exists = change.first_before_state != ABSENT_REVISION
            if before_exists:
                if not change.first_before_artifact_id:
                    raise RuntimeError(
                        f"Run change lacks its first preimage artifact: {relative}"
                    )
                before = artifact_store.read_internal_text(
                    run_id,
                    change.first_before_artifact_id,
                )
            else:
                before = ""
            after_exists = current_state != ABSENT_REVISION
            after = target.read_bytes().decode("utf-8") if after_exists else ""
            rendered.append(
                unified_text_diff(
                    relative,
                    before,
                    after,
                    before_exists=before_exists,
                    after_exists=after_exists,
                )
            )
        return "".join(rendered)


def _effect_from_event(event, outcome):
    structured = dict(outcome.get("structured", {}) or {})
    return {
        "tool_call_id": str(outcome.get("tool_call_id", "")),
        "tool": str(outcome.get("tool_name", "")),
        "status": str(outcome.get("status", "error")),
        "execution_state": str(outcome.get("execution_state", "failed")),
        "side_effect_state": str(outcome.get("side_effect_state", "unknown")),
        "affected_paths": tuple(str(path) for path in outcome.get("affected_paths", ())),
        "effect_scope": str(outcome.get("effect_scope", "none")),
        "event_sequence": int(event.sequence),
        "path_transitions": tuple(
            dict(item) for item in structured.get("path_transitions", ())
        ),
    }


def verification_is_current(
    record,
    mutation_sequence,
    changed_path_states,
    command,
):
    expected_sequence = int(mutation_sequence)
    expected_states = dict(changed_path_states)
    return bool(
        int(record.get("started_workspace_mutation_sequence", -1))
        == int(record.get("finished_workspace_mutation_sequence", -2))
        == expected_sequence
        and dict(record.get("started_changed_path_states", {}))
        == dict(record.get("finished_changed_path_states", {}))
        == expected_states
        and str(record.get("command", "")) == str(command)
    )


@dataclass
class RunEvidence:
    """Only current decision state; full history remains in RunLog."""

    change_set: RunChangeSet = field(default_factory=RunChangeSet)
    uncertain_effects: list[dict] = field(default_factory=list)
    latest_verification: dict | None = None
    last_workspace_mutation_sequence: int = 0

    def apply_event(self, event):
        if event.kind == "verification_result":
            self.latest_verification = dict(event.payload)
            changes = event.payload["workspace_changes"]
            if changes is None or changes:
                self._record_effect(event, {
                    "tool_call_id": event.event_id,
                    "tool_name": "verification",
                    "status": "error",
                    "execution_state": "completed",
                    "side_effect_state": "unknown",
                    "affected_paths": changes or (),
                    "effect_scope": "workspace",
                })
            return self
        if event.kind not in {"tool_exchange", "tool_settlement"}:
            return self
        outcome = event.payload["outcome"]
        structured = outcome.get("structured", {})
        if outcome["tool_name"] == "verify" and "verification" in structured:
            self.latest_verification = dict(structured["verification"])
        if outcome["tool_name"] == "read_file":
            path, revision = structured.get("path"), structured.get("revision")
            missing = (outcome.get("failure") or {}).get("code") == "missing_path"
            known = self.change_set.files.get(path)
            if (known is not None and revision
                    and (outcome["status"] == "success" or (missing and revision == ABSENT_REVISION))
                    and revision != known.current_after_state):
                known.current_after_state = revision
                known.last_mutation_sequence = event.sequence
                known.external_change_observed = True
                self.last_workspace_mutation_sequence = event.sequence
        if outcome["side_effect_state"] != "none":
            self._record_effect(event, outcome)
        return self

    def _record_effect(self, event, outcome):
        effect = _effect_from_event(event, outcome)
        if effect["effect_scope"] in WORKSPACE_SCOPES:
            self.last_workspace_mutation_sequence = event.sequence
        if effect["side_effect_state"] in {"changed", "partial"}:
            paths = {item["path"] for item in effect["path_transitions"]}
            if set(effect["affected_paths"]) - paths:
                # A diagnostic command can report changed paths without edit receipts.
                # Keep it unresolved instead of inventing a reconstructable change.
                effect["side_effect_state"] = "unknown"
            else:
                self.change_set.apply_effect(effect)
        if effect["side_effect_state"] in {"partial", "unknown"}:
            self.uncertain_effects.append(effect)

    @property
    def changed_paths(self):
        return list(self.change_set.net_changed_paths)

    @property
    def external_paths(self):
        return tuple(path for path in self.change_set.net_changed_paths
                     if self.change_set.files[path].external_change_observed)

    @property
    def touched_paths(self):
        return list(self.change_set.touched_paths)

    @property
    def has_net_workspace_change(self):
        return bool(self.change_set.net_changed_paths)

    def latest_verification_for_state(self, mutation_sequence, changed_path_states, command):
        record = self.latest_verification
        if record is not None and verification_is_current(
            record, mutation_sequence, changed_path_states, command,
        ):
            return record
        return None

    def partial_workspace_effects(self):
        return [effect for effect in self.uncertain_effects
                if effect["effect_scope"] in WORKSPACE_SCOPES
                and effect["side_effect_state"] == "partial"]

    def unverifiable_effects(self):
        return [effect for effect in self.uncertain_effects
                if effect["side_effect_state"] == "unknown"
                or effect["effect_scope"] not in WORKSPACE_SCOPES
                or not effect["affected_paths"]]

    def to_dict(self):
        return {
            "change_set": self.change_set.to_dict(),
            "uncertain_effects": [
                {**effect, "affected_paths": list(effect["affected_paths"]),
                 "path_transitions": [dict(item) for item in effect["path_transitions"]]}
                for effect in self.uncertain_effects
            ],
            "latest_verification": dict(self.latest_verification) if self.latest_verification else None,
            "last_workspace_mutation_sequence": self.last_workspace_mutation_sequence,
        }
