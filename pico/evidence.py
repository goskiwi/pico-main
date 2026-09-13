"""Current file changes and uncertain effects rebuilt from RunLog."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .mutations import ABSENT_REVISION, file_revision

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
            "current_after_state": self.current_after_state,
            "last_mutation_sequence": self.last_mutation_sequence,
            "net_changed": self.net_changed,
            "external_change_observed": self.external_change_observed,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "path",
            "first_before_state",
            "current_after_state",
            "last_mutation_sequence",
            "net_changed",
            "external_change_observed",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid checkpoint FileChange")
        change = cls(
            path=str(value["path"]),
            first_before_state=str(value["first_before_state"]),
            current_after_state=str(value["current_after_state"]),
            last_mutation_sequence=int(value["last_mutation_sequence"]),
            external_change_observed=bool(value["external_change_observed"]),
        )
        if bool(value["net_changed"]) != change.net_changed:
            raise ValueError("checkpoint FileChange net state is inconsistent")
        return change

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

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {
            "touched_paths",
            "net_changed_paths",
            "files",
        }:
            raise ValueError("invalid checkpoint RunChangeSet")
        if not isinstance(value["files"], dict):
            raise TypeError("checkpoint RunChangeSet files must be an object")
        result = cls(
            files={
                str(path): FileChange.from_dict(change)
                for path, change in value["files"].items()
            }
        )
        if list(value["touched_paths"]) != list(result.touched_paths):
            raise ValueError("checkpoint touched paths are inconsistent")
        if list(value["net_changed_paths"]) != list(result.net_changed_paths):
            raise ValueError("checkpoint changed paths are inconsistent")
        return result

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


@dataclass
class RunEvidence:
    """Only current decision state; full history remains in RunLog."""

    change_set: RunChangeSet = field(default_factory=RunChangeSet)
    uncertain_effects: list[dict] = field(default_factory=list)

    def apply_event(self, event):
        if event.kind not in {"tool_exchange", "tool_settlement"}:
            return self
        outcome = event.payload["outcome"]
        structured = outcome.get("structured", {})
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
        if outcome["side_effect_state"] != "none":
            self._record_effect(event, outcome)
        return self

    def _record_effect(self, event, outcome):
        effect = _effect_from_event(event, outcome)
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

    def to_dict(self):
        return {
            "change_set": self.change_set.to_dict(),
            "uncertain_effects": [
                {**effect, "affected_paths": list(effect["affected_paths"]),
                 "path_transitions": [dict(item) for item in effect["path_transitions"]]}
                for effect in self.uncertain_effects
            ],
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "change_set",
            "uncertain_effects",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid checkpoint RunEvidence")
        effects = value["uncertain_effects"]
        if not isinstance(effects, list):
            raise TypeError("checkpoint uncertain effects must be a list")
        normalized = []
        for effect in effects:
            expected_effect = {
                "tool_call_id",
                "tool",
                "status",
                "execution_state",
                "side_effect_state",
                "affected_paths",
                "effect_scope",
                "event_sequence",
                "path_transitions",
            }
            if not isinstance(effect, dict) or set(effect) != expected_effect:
                raise ValueError("invalid checkpoint uncertain effect")
            if not isinstance(effect["affected_paths"], list) or not isinstance(
                effect["path_transitions"], list
            ):
                raise TypeError("checkpoint uncertain effect collections are invalid")
            normalized.append(
                {
                    **effect,
                    "affected_paths": tuple(effect.get("affected_paths", ())),
                    "path_transitions": tuple(
                        dict(item) for item in effect.get("path_transitions", ())
                    ),
                }
            )
        return cls(
            change_set=RunChangeSet.from_dict(value["change_set"]),
            uncertain_effects=normalized,
        )
