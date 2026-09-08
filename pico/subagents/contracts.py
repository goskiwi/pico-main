"""Strict contracts for one synchronous Pico child."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..workspace import normalize_relative_file


def planned_worktree_path(parent_run_id, label):
    parent_run_id = str(parent_run_id)
    label = str(label)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", parent_run_id):
        raise ValueError("invalid Parent Run id for Worktree planning")
    if not re.fullmatch(r"(?:child|integration)_[a-f0-9]{12}", label):
        raise ValueError("invalid planned Worktree label")
    return str(
        Path(tempfile.gettempdir()).resolve()
        / "pico-worktrees"
        / parent_run_id
        / label
    )


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
class ChildLaunch:
    child_id: str
    base_sha: str
    verification_command: str
    worktree_path: str

    def __post_init__(self):
        if not re.fullmatch(r"child_[a-f0-9]{12}", self.child_id):
            raise ValueError("invalid Child launch id")
        for value in (
            self.base_sha,
            self.verification_command,
            self.worktree_path,
        ):
            if not isinstance(value, str):
                raise TypeError("Child launch fields must be text")

    def validate_shape_for(self, spec):
        if spec.role == "explore" and any(
            (self.base_sha, self.verification_command, self.worktree_path)
        ):
            raise ValueError("Explore Child launch cannot contain implementation state")
        if spec.role == "implement" and not all(
            (self.base_sha, self.verification_command, self.worktree_path)
        ):
            raise ValueError("Implement Child launch is incomplete")
        return self

    def validate_for(self, spec, parent_run_id):
        self.validate_shape_for(spec)
        if spec.role == "implement" and self.worktree_path != planned_worktree_path(
            parent_run_id,
            self.child_id,
        ):
            raise ValueError("Implement Child Worktree path is not canonical")
        return self

    def to_dict(self):
        return {
            "child_id": self.child_id,
            "base_sha": self.base_sha,
            "verification_command": self.verification_command,
            "worktree_path": self.worktree_path,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "child_id",
            "base_sha",
            "verification_command",
            "worktree_path",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid Child launch fields")
        return cls(**{key: value[key] for key in expected})


@dataclass(frozen=True)
class ChildIntegration:
    child_id: str
    base_sha: str
    verification_command: str
    worktree_path: str

    def __post_init__(self):
        if not re.fullmatch(r"child_[a-f0-9]{12}", self.child_id):
            raise ValueError("invalid Child integration id")
        if not all(
            isinstance(value, str) and value
            for value in (
                self.base_sha,
                self.verification_command,
                self.worktree_path,
            )
        ):
            raise ValueError("Child integration plan is incomplete")

    def to_dict(self):
        return {
            "child_id": self.child_id,
            "base_sha": self.base_sha,
            "verification_command": self.verification_command,
            "worktree_path": self.worktree_path,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "child_id",
            "base_sha",
            "verification_command",
            "worktree_path",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid Child integration fields")
        return cls(**{key: value[key] for key in expected})

    def validate_for(self, record, parent_run_id):
        path = Path(self.worktree_path)
        expected_parent = Path(
            planned_worktree_path(parent_run_id, record.child_id)
        ).parent
        if (
            self.child_id != record.child_id
            or self.base_sha != record.base_sha
            or path.parent != expected_parent
            or not re.fullmatch(r"integration_[a-f0-9]{12}", path.name)
        ):
            raise ValueError("Child integration plan does not match its receipt")
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
    parent_call_id: str
    spec: ChildSpec
    base_sha: str = ""
    verification_command: str = ""
    worktree_path: str = ""
    result: ChildSuccess | ChildFailure | None = None

    def __post_init__(self):
        ChildLaunch(
            self.child_id,
            self.base_sha,
            self.verification_command,
            self.worktree_path,
        ).validate_shape_for(self.spec)
        if not self.parent_call_id:
            raise ValueError("Child record requires its parent call id")
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

    def record_for_call(self, call_id):
        matches = [
            record
            for record in self.records.values()
            if record.parent_call_id == call_id
        ]
        if len(matches) > 1:
            raise ValueError("multiple Children belong to one parent call")
        return matches[0] if matches else None

    def check_started(self, call, payload, parent_run_id):
        operation = payload["operation"]
        if call.name == "integrate_child":
            integration = ChildIntegration.from_dict(operation)
            record = self.record(call.args["child_id"])
            patch = record.completed().patch
            if (
                patch is None
                or patch.integrated
            ):
                raise ValueError("Child integration plan does not match its receipt")
            integration.validate_for(record, parent_run_id)
            return
        if call.name != "delegate":
            if operation:
                raise ValueError("ordinary tool_started operation must be empty")
            return
        spec = ChildSpec.model_validate(call.args)
        launch = ChildLaunch.from_dict(operation).validate_for(
            spec,
            parent_run_id,
        )
        if launch.child_id in self.records or self.record_for_call(call.call_id):
            raise ValueError("Child launch identity is already recorded")

    def apply_started(self, call, payload, parent_run_id):
        if call.name != "delegate":
            return
        spec = ChildSpec.model_validate(call.args)
        launch = ChildLaunch.from_dict(payload["operation"]).validate_for(
            spec,
            parent_run_id,
        )
        self.records[launch.child_id] = ChildRecord(
            launch.child_id,
            call.call_id,
            spec,
            launch.base_sha,
            launch.verification_command,
            launch.worktree_path,
        )

    def _delegate_result(self, call, outcome):
        receipt = outcome["structured"]
        record = self.record_for_call(call.call_id)
        if record is None:
            if outcome["execution_state"] != "not_started":
                raise ValueError("executed delegate result requires a Child launch")
            if "child_id" in receipt:
                raise ValueError("unstarted delegate result cannot name a Child")
            return None
        spec = ChildSpec.model_validate(call.args)
        child_id = receipt.get("child_id")
        if child_id != record.child_id or receipt.get("role") != spec.role:
            raise ValueError("invalid Child receipt identity")
        child_run_id = receipt.get("child_run_id", "")
        if outcome["status"] != "success":
            return ChildFailure(outcome["failure"]["detail"], child_run_id)
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
        if base and base != record.base_sha:
            raise ValueError("Child receipt base does not match its launch")
        return ChildSuccess(child_run_id, patch)

    def check_result(self, call, outcome):
        if call.name == "delegate":
            self._delegate_result(call, outcome)
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
            result = self._delegate_result(call, outcome)
            record = self.record_for_call(call.call_id)
            if record is not None and result is not None:
                record.result = result
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
