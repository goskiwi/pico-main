"""Runtime-owned task requirements; live state belongs to RunProjection."""

from __future__ import annotations

from dataclasses import dataclass

from .workspace import normalize_relative_file

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"


@dataclass(frozen=True)
class TaskContract:
    """Immutable goal, write scope, and completion requirements for one Run."""

    goal: str
    allows_workspace_mutation: bool
    verify_changes: bool
    allowed_write_paths: tuple[str, ...] | None = None

    def __post_init__(self):
        if not isinstance(self.goal, str):
            raise TypeError("task contract goal must be a string")
        if not isinstance(self.allows_workspace_mutation, bool):
            raise TypeError("allows_workspace_mutation must be a boolean")
        if not isinstance(self.verify_changes, bool):
            raise TypeError("verify_changes must be a boolean")
        if self.allowed_write_paths is not None:
            if not isinstance(self.allowed_write_paths, (list, tuple)):
                raise TypeError("allowed_write_paths must be a sequence or null")
            if any(not isinstance(path, str) for path in self.allowed_write_paths):
                raise TypeError("allowed_write_paths entries must be strings")
        object.__setattr__(
            self,
            "allowed_write_paths",
            None
            if self.allowed_write_paths is None
            else tuple(
                normalize_relative_file(path) for path in self.allowed_write_paths
            ),
        )
        self.validate()

    def validate(self):
        if not self.goal.strip():
            raise ValueError("task contract requires a goal")
        if not self.allows_workspace_mutation and self.allowed_write_paths:
            raise ValueError("non-mutating contract cannot allow write paths")
        if self.allowed_write_paths is not None and len(
            set(self.allowed_write_paths)
        ) != len(self.allowed_write_paths):
            raise ValueError("allowed_write_paths must be unique")
        return self

    @classmethod
    def from_dict(cls, value):
        expected = {
            "goal",
            "allows_workspace_mutation",
            "verify_changes",
            "allowed_write_paths",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid task contract fields")
        paths = value["allowed_write_paths"]
        if paths is not None and not isinstance(paths, list):
            raise TypeError("allowed_write_paths must be a list or null")
        return cls(
            goal=value["goal"],
            allows_workspace_mutation=value["allows_workspace_mutation"],
            verify_changes=value["verify_changes"],
            allowed_write_paths=None if paths is None else tuple(paths),
        )

    def to_dict(self):
        self.validate()
        return {
            "goal": self.goal,
            "allows_workspace_mutation": self.allows_workspace_mutation,
            "verify_changes": self.verify_changes,
            "allowed_write_paths": (
                None
                if self.allowed_write_paths is None
                else list(self.allowed_write_paths)
            ),
        }
