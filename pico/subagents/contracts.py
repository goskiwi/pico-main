"""Strict contracts for one synchronous Pico child."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..workspace import normalize_relative_file


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChildSpec(StrictModel):
    role: Literal["explore", "implement"]
    task: str = Field(min_length=1, max_length=6000)
    allowed_write_paths: tuple[str, ...] = ()

    @field_validator("task")
    @classmethod
    def normalize_task(cls, value):
        return str(value).strip()

    @field_validator("allowed_write_paths")
    @classmethod
    def validate_write_paths(cls, value):
        normalized = tuple(normalize_relative_file(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_write_paths must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_role_contract(self):
        if self.role == "explore" and self.allowed_write_paths:
            raise ValueError("explore children cannot declare write paths")
        if self.role == "implement" and not self.allowed_write_paths:
            raise ValueError("implement children require allowed_write_paths")
        return self


@dataclass(frozen=True)
class ChildPatch:
    changed_paths: tuple[str, ...]
    path: str
    sha256: str
    integrated: bool = False

    def __post_init__(self):
        if not self.changed_paths or not self.path or not self.sha256:
            raise ValueError("Child patch requires paths, location, and digest")


@dataclass(frozen=True)
class ChildSuccess:
    child_run_id: str
    patch: ChildPatch | None = None

    def __post_init__(self):
        if not self.child_run_id:
            raise ValueError("successful Child requires a Run id")


@dataclass(frozen=True)
class ChildFailure:
    error: str
    child_run_id: str = ""

    def __post_init__(self):
        if not self.error:
            raise ValueError("failed Child requires an error")


@dataclass
class ChildRecord:
    child_id: str
    spec: ChildSpec
    base_sha: str = ""
    result: ChildSuccess | ChildFailure | None = None

    def __post_init__(self):
        if isinstance(self.result, ChildSuccess):
            self.completed()

    @property
    def status(self):
        if self.result is None:
            return "running"
        return "completed" if isinstance(self.result, ChildSuccess) else "failed"

    def completed(self):
        if not isinstance(self.result, ChildSuccess):
            raise TypeError(f"Child is not completed: {self.child_id}")
        if self.spec.role == "implement":
            if not self.base_sha or self.result.patch is None:
                raise ValueError(f"Implement Child requires base and patch: {self.child_id}")
        elif self.result.patch is not None:
            raise ValueError(f"Explore Child cannot contain a patch: {self.child_id}")
        return self.result

    def mark_integrated(self):
        success = self.completed()
        if success.patch is None:
            raise ValueError(f"Child has no patch: {self.child_id}")
        self.result = replace(
            success,
            patch=replace(success.patch, integrated=True),
        )
