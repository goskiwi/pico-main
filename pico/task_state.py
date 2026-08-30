"""Runtime-owned task requirements, working notes, and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, field

from .features.memory import WorkingState
from .workspace import normalize_relative_file

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
TASK_STATUSES = frozenset({STATUS_RUNNING, STATUS_COMPLETED, STATUS_STOPPED})
TASK_KINDS = frozenset({"read_only", "modify"})
STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"


@dataclass
class TaskContract:
    """Immutable goal, write scope, and completion requirements for one Run."""

    goal: str
    task_kind: str
    requires_workspace_change: bool
    requires_verification: bool
    allowed_write_paths: tuple[str, ...] | None = None

    def __post_init__(self):
        if not isinstance(self.goal, str):
            raise TypeError("task contract goal must be a string")
        if not isinstance(self.task_kind, str):
            raise TypeError("task contract task_kind must be a string")
        if self.allowed_write_paths is not None:
            if not isinstance(self.allowed_write_paths, (list, tuple)):
                raise TypeError("allowed_write_paths must be a sequence or null")
            if any(not isinstance(path, str) for path in self.allowed_write_paths):
                raise TypeError("allowed_write_paths entries must be strings")
        self.allowed_write_paths = (
            None
            if self.allowed_write_paths is None
            else tuple(
                normalize_relative_file(path) for path in self.allowed_write_paths
            )
        )
        self.validate()

    def validate(self):
        if not self.goal.strip():
            raise ValueError("task contract requires a goal")
        if self.task_kind not in TASK_KINDS:
            raise ValueError(f"invalid task kind: {self.task_kind}")
        if not isinstance(self.requires_workspace_change, bool):
            raise TypeError("requires_workspace_change must be a boolean")
        if not isinstance(self.requires_verification, bool):
            raise TypeError("requires_verification must be a boolean")
        if self.task_kind == "read_only" and self.requires_workspace_change:
            raise ValueError("read-only task cannot require workspace changes")
        if self.task_kind == "read_only" and self.allowed_write_paths:
            raise ValueError("read-only task cannot allow write paths")
        if self.allowed_write_paths is not None and len(
            set(self.allowed_write_paths)
        ) != len(self.allowed_write_paths):
            raise ValueError("allowed_write_paths must be unique")
        return self

    @classmethod
    def from_dict(cls, value):
        expected = {
            "goal",
            "task_kind",
            "requires_workspace_change",
            "requires_verification",
            "allowed_write_paths",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid task contract fields")
        paths = value["allowed_write_paths"]
        if paths is not None and not isinstance(paths, list):
            raise TypeError("allowed_write_paths must be a list or null")
        return cls(
            goal=value["goal"],
            task_kind=value["task_kind"],
            requires_workspace_change=value["requires_workspace_change"],
            requires_verification=value["requires_verification"],
            allowed_write_paths=None if paths is None else tuple(paths),
        )

    def to_dict(self):
        self.validate()
        return {
            "goal": self.goal,
            "task_kind": self.task_kind,
            "requires_workspace_change": self.requires_workspace_change,
            "requires_verification": self.requires_verification,
            "allowed_write_paths": (
                None
                if self.allowed_write_paths is None
                else list(self.allowed_write_paths)
            ),
        }


@dataclass
class TaskLifecycle:
    status: str = STATUS_RUNNING
    stop_reason: str = ""
    final_answer: str = ""

    def validate(self):
        if self.status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {self.status}")
        if self.status == STATUS_RUNNING:
            if self.stop_reason or self.final_answer:
                raise ValueError("running task cannot have terminal fields")
        elif self.status == STATUS_COMPLETED:
            if self.stop_reason != STOP_REASON_FINAL_ANSWER_RETURNED:
                raise ValueError(
                    "completed task requires final_answer_returned stop_reason"
                )
            if not self.final_answer.strip():
                raise ValueError("completed task requires final_answer")
        elif not self.stop_reason:
            raise ValueError("stopped task requires stop_reason")
        return self

    def apply_event(self, event):
        if event.kind == "assistant_final":
            self.status = STATUS_COMPLETED
            self.stop_reason = str(event.payload["stop_reason"])
            self.final_answer = str(event.payload.get("content", ""))
        elif event.kind == "run_stopped":
            self.status = STATUS_STOPPED
            self.stop_reason = str(event.payload.get("stop_reason", ""))
            self.final_answer = str(event.payload.get("content", ""))
        return self.validate()

    def to_dict(self):
        self.validate()
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
        }


@dataclass
class TaskState:
    contract: TaskContract
    working: WorkingState = field(default_factory=WorkingState)
    lifecycle: TaskLifecycle = field(default_factory=TaskLifecycle)

    @classmethod
    def create(cls, contract):
        if not isinstance(contract, TaskContract):
            raise TypeError("task state requires a TaskContract")
        return cls(contract=contract)

    def apply_event(self, event):
        self.working.apply_event(event)
        self.lifecycle.apply_event(event)
        return self

    def to_dict(self):
        return {
            "contract": self.contract.to_dict(),
            "working": self.working.to_dict(),
            "lifecycle": self.lifecycle.to_dict(),
        }
