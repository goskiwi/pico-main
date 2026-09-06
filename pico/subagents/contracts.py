"""Strict contracts for one synchronous Pico child."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
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
    sha256: str
    integrated: bool = False

    def __post_init__(self):
        if not self.changed_paths or not self.sha256:
            raise ValueError("Child patch requires paths and digest")


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
        if self.result.patch is not None:
            if self.spec.role != "implement":
                raise ValueError(f"Explore Child cannot contain a patch: {self.child_id}")
            if not self.base_sha:
                raise ValueError(f"Child patch requires a base: {self.child_id}")
        return self.result

    def mark_integrated(self):
        success = self.completed()
        if success.patch is None:
            raise ValueError(f"Child has no patch: {self.child_id}")
        self.result = replace(
            success,
            patch=replace(success.patch, integrated=True),
        )


@dataclass
class ChildState:
    """Parent-event-derived Child receipts; no model, filesystem or Worktree access."""

    records: dict[str, ChildRecord] = field(default_factory=dict)

    def record(self, child_id):
        try:
            return self.records[child_id]
        except KeyError:
            raise ValueError(f"unknown child: {child_id}") from None

    def _delegate_record(self, call, outcome):
        receipt = outcome["structured"]
        if "child_id" not in receipt and outcome["status"] != "success":
            return None
        spec = ChildSpec.model_validate(call.args)
        child_id = receipt["child_id"]
        if not isinstance(child_id, str) or not re.fullmatch(
            r"child_[a-f0-9]{12}", child_id
        ):
            raise ValueError("invalid Child receipt id")
        if child_id in self.records or receipt["role"] != spec.role:
            raise ValueError("invalid Child receipt identity")
        child_run_id = receipt.get("child_run_id", "")
        if outcome["status"] != "success":
            return ChildRecord(
                child_id, spec, result=ChildFailure(receipt["error"], child_run_id)
            )
        if receipt["status"] != "completed":
            raise ValueError("invalid completed Child receipt")
        patch = None
        base = ""
        if spec.role == "implement" and "patch" in receipt:
            raw = receipt["patch"]
            base = raw["base_sha"]
            paths = raw["changed_paths"]
            if not isinstance(paths, list) or not all(
                isinstance(p, str) for p in paths
            ):
                raise ValueError("invalid Child patch paths")
            paths = tuple(normalize_relative_file(p) for p in paths)
            if not set(paths) <= set(spec.allowed_write_paths):
                raise ValueError("persisted Child paths exceed the delegate call scope")
            if not all(
                isinstance(value, str) and value
                for value in (base, raw["sha256"], child_run_id)
            ):
                raise ValueError("invalid persisted delegate receipt")
            patch = ChildPatch(paths, raw["sha256"])
        elif "patch" in receipt:
            raise ValueError("Explore Child cannot contain a patch")
        return ChildRecord(child_id, spec, base, ChildSuccess(child_run_id, patch))

    def check_result(self, call, outcome):
        if call.name == "delegate":
            self._delegate_record(call, outcome)
        elif call.name == "integrate_child" and (
            outcome["status"] == "success"
            or outcome["structured"].get("status") == "integrated"
        ):
            child_id = call.args["child_id"]
            if (
                set(call.args) != {"child_id"}
                or outcome["structured"]["child_id"] != child_id
            ):
                raise ValueError("invalid persisted integration receipt")
            record = self.record(child_id)
            patch = record.completed().patch
            if patch is None:
                raise ValueError("Child has no patch")
            receipt = outcome["structured"]
            if (
                receipt.get("status") != "integrated"
                or receipt.get("base_sha") != record.base_sha
                or receipt.get("changed_paths") != list(patch.changed_paths)
                or outcome["status"] not in {"success", "partial_success"}
            ):
                raise ValueError("integration receipt does not match the Child patch")

    def apply_result(self, call, outcome):
        if call.name == "delegate":
            record = self._delegate_record(call, outcome)
            if record is not None:
                self.records[record.child_id] = record
        elif call.name == "integrate_child" and outcome["structured"].get("status") == "integrated":
            self.record(call.args["child_id"]).mark_integrated()

    def completion_issue(self):
        unapplied = sorted(
            key
            for key, record in self.records.items()
            if isinstance(record.result, ChildSuccess)
            and record.result.patch is not None
            and not record.result.patch.integrated
        )
        return (
            "completed implementation patches are not integrated: "
            + ", ".join(unapplied)
            if unapplied
            else ""
        )
