"""Strict contracts for one parent-owned subtask graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..workspace import normalize_relative_file


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubtaskSpec(StrictModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: Literal["explore", "implement"]
    prompt: str = Field(min_length=1, max_length=6000)
    depends_on: tuple[str, ...] = ()
    allowed_write_paths: tuple[str, ...] = ()
    max_tool_executions: int = Field(default=12, ge=1, le=12)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value):
        return str(value).strip()

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, value):
        normalized = tuple(str(item).strip() for item in value)
        if any(not item for item in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("depends_on must contain unique non-empty task ids")
        return normalized

    @field_validator("allowed_write_paths")
    @classmethod
    def validate_write_paths(cls, value):
        normalized = tuple(normalize_relative_file(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_write_paths must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_kind_contract(self):
        if self.task_id in self.depends_on:
            raise ValueError("subtask cannot depend on itself")
        if self.kind == "explore" and self.allowed_write_paths:
            raise ValueError("explore subtasks cannot declare write paths")
        if self.kind == "implement" and not self.allowed_write_paths:
            raise ValueError("implement subtasks require allowed_write_paths")
        return self


@dataclass
class SubtaskRecord:
    spec: SubtaskSpec
    status: Literal["pending", "running", "completed", "failed", "blocked"] = (
        "pending"
    )
    child_run_id: str = ""
    base_sha: str = ""
    changed_paths: tuple[str, ...] = ()
    patch_path: str = ""
    patch_sha256: str = ""
    error: str = ""
    applied: bool = False
