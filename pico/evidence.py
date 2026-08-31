"""Small event projection for observations, effects, net changes, and verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .mutations import ABSENT_REVISION, file_revision, unified_text_diff

OBSERVATION_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "read_artifact",
        "search",
        "delegate",
    }
)
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
            after = target.read_text(encoding="utf-8") if after_exists else ""
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


def _observation_from_event(event, outcome):
    structured = dict(outcome.get("structured", {}) or {})
    return {
        "tool_call_id": str(outcome.get("tool_call_id", "")),
        "tool": str(outcome.get("tool_name", "")),
        "status": str(outcome.get("status", "error")),
        "execution_state": str(outcome.get("execution_state", "failed")),
        "event_sequence": int(event.sequence),
        "path": str(structured.get("path", "")),
    }


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


def verification_is_current(record, mutation_sequence, changed_path_states):
    expected_sequence = int(mutation_sequence)
    expected_states = dict(changed_path_states)
    return bool(
        int(record.get("started_workspace_mutation_sequence", -1))
        == int(record.get("finished_workspace_mutation_sequence", -2))
        == expected_sequence
        and dict(record.get("started_changed_path_states", {}))
        == dict(record.get("finished_changed_path_states", {}))
        == expected_states
    )


@dataclass
class RunEvidence:
    observations: list[dict] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)
    verifications: list[dict] = field(default_factory=list)
    change_set: RunChangeSet = field(default_factory=RunChangeSet)

    def apply_event(self, event):
        if event.kind == "verification_result":
            self.verifications.append(dict(event.payload))
            return self
        if event.kind != "tool_result":
            return self
        outcome = dict(event.payload.get("outcome", {}) or {})
        if str(outcome.get("tool_name", "")) in OBSERVATION_TOOLS:
            self.observations.append(_observation_from_event(event, outcome))
        if str(outcome.get("side_effect_state", "none")) != "none":
            effect = _effect_from_event(event, outcome)
            self.effects.append(effect)
            self.change_set.apply_effect(effect)
        return self

    @property
    def successful_observation_count(self):
        return sum(
            item["status"] == "success" and item["execution_state"] == "completed"
            for item in self.observations
        )

    @property
    def changed_paths(self):
        return list(self.change_set.net_changed_paths)

    @property
    def touched_paths(self):
        return list(self.change_set.touched_paths)

    @property
    def has_net_workspace_change(self):
        return bool(self.change_set.net_changed_paths)

    @property
    def last_workspace_mutation_sequence(self):
        return max(
            (
                int(item["event_sequence"])
                for item in self.effects
                if item["effect_scope"] in WORKSPACE_SCOPES
            ),
            default=0,
        )

    def latest_verification_for_state(self, mutation_sequence, changed_path_states):
        return next(
            (
                record
                for record in reversed(self.verifications)
                if verification_is_current(
                    record,
                    mutation_sequence,
                    changed_path_states,
                )
            ),
            None,
        )

    def _repair_after(self, index, effect):
        if effect["side_effect_state"] != "partial" or not effect["affected_paths"]:
            return None
        affected = set(effect["affected_paths"])
        return next(
            (
                later
                for later in self.effects[index + 1 :]
                if later["status"] == "success"
                and later["side_effect_state"] == "changed"
                and later["effect_scope"] == "workspace"
                and affected.issubset(set(later["affected_paths"]))
            ),
            None,
        )

    def repaired_partials_requiring_verification(self):
        return [
            effect
            for index, effect in enumerate(self.effects)
            if effect["effect_scope"] in WORKSPACE_SCOPES
            and self._repair_after(index, effect) is not None
        ]

    def unrepaired_uncertain_effects(self):
        return [
            effect
            for index, effect in enumerate(self.effects)
            if effect["side_effect_state"] in {"partial", "unknown"}
            and (
                effect["side_effect_state"] == "unknown"
                or self._repair_after(index, effect) is None
            )
        ]

    def unresolved_effects(self, current_verification=None):
        unresolved = []
        for index, effect in enumerate(self.effects):
            if effect["side_effect_state"] not in {"partial", "unknown"}:
                continue
            if effect["side_effect_state"] == "unknown" or not effect["affected_paths"]:
                unresolved.append(effect)
                continue
            repair = self._repair_after(index, effect)
            repaired_and_verified = bool(
                repair is not None
                and current_verification is not None
                and current_verification.get("status") == "passed"
                and int(current_verification["finished_workspace_mutation_sequence"])
                >= int(repair["event_sequence"])
            )
            if not repaired_and_verified:
                unresolved.append(effect)
        return unresolved

    def to_dict(self):
        return {
            "successful_observation_count": self.successful_observation_count,
            "observations": [dict(item) for item in self.observations],
            "effects": [
                {
                    **dict(item),
                    "affected_paths": list(item["affected_paths"]),
                    "path_transitions": [
                        dict(transition) for transition in item["path_transitions"]
                    ],
                }
                for item in self.effects
            ],
            "change_set": self.change_set.to_dict(),
            "verifications": [dict(item) for item in self.verifications],
        }
