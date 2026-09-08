"""Runtime-owned requirements retained across Run recovery."""

from dataclasses import dataclass
from typing import Literal

from .workspace import normalize_relative_file

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"


@dataclass(frozen=True)
class WriteScope:
    mode: Literal["none", "workspace", "paths"]
    paths: tuple[str, ...] = ()

    def __post_init__(self):
        if self.mode not in {"none", "workspace", "paths"}:
            raise ValueError("invalid write scope mode")
        if not isinstance(self.paths, tuple) or any(not isinstance(p, str) for p in self.paths):
            raise TypeError("write scope paths must be a tuple of strings")
        normalized = tuple(normalize_relative_file(p) for p in self.paths)
        if len(set(normalized)) != len(normalized):
            raise ValueError("write scope paths must be unique")
        if (self.mode == "paths") != bool(normalized):
            raise ValueError("only paths mode requires a non-empty path list")
        object.__setattr__(self, "paths", normalized)

    @classmethod
    def from_policy(cls, mode, paths):
        if mode == "ask" or paths == ():
            return cls("none")
        return cls("workspace") if paths is None else cls("paths", tuple(paths))

    def allowed_paths(self):
        return None if self.mode == "workspace" else self.paths

    def to_dict(self):
        return {"mode": self.mode, "paths": list(self.paths)}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"mode", "paths"}:
            raise ValueError("invalid write scope fields")
        if not isinstance(value["paths"], list):
            raise TypeError("write scope paths must be a list")
        return cls(value["mode"], tuple(value["paths"]))


@dataclass(frozen=True)
class TaskContract:
    goal: str
    write_scope: WriteScope
    verify_changes: bool

    def __post_init__(self):
        self.validate()

    def validate(self):
        if not isinstance(self.goal, str):
            raise TypeError("task contract goal must be a string")
        if not self.goal.strip():
            raise ValueError("task contract requires a goal")
        if not isinstance(self.write_scope, WriteScope):
            raise TypeError("task contract requires WriteScope")
        if not isinstance(self.verify_changes, bool):
            raise TypeError("verify_changes must be a boolean")
        return self

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {"goal", "write_scope", "verify_changes"}:
            raise ValueError("invalid task contract fields")
        return cls(value["goal"], WriteScope.from_dict(value["write_scope"]), value["verify_changes"])

    def to_dict(self):
        return {"goal": self.goal, "write_scope": self.write_scope.to_dict(),
                "verify_changes": self.verify_changes}
