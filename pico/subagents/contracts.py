"""Strict contracts for one synchronous Pico child."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class ChildRecord:
    child_id: str
    spec: ChildSpec
    status: Literal["running", "completed", "failed"] = "running"
    child_run_id: str = ""
    base_sha: str = ""
    changed_paths: tuple[str, ...] = ()
    patch_path: str = ""
    patch_sha256: str = ""
    error: str = ""
    integrated: bool = False
