"""Recoverable single-writer Session state.

The Session is Pico's only durable task state. It stores conversation history,
tool phases, verification state and mutation receipts together so recovery does
not need an event log plus a separate projection.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_verification() -> dict:
    return {
        "status": "not_run",
        "command": "",
        "operation_id": "",
        "workspace_state": None,
    }


def new_loop_control() -> dict:
    return {
        "invalid_outputs": 0,
        "completion_blocks": 0,
        "last_completion_error": "",
        "denied": [],
    }


@dataclass
class Session:
    """One atomic snapshot for a conversation and its current task."""

    store: object = field(repr=False, compare=False)
    id: str
    workspace_root: str
    history: list[dict] = field(default_factory=list)
    summary: str = ""
    covered: int = 0
    observed: int = 0
    request_start: int = 0
    run: dict = field(default_factory=dict)
    loop_control: dict = field(default_factory=new_loop_control)
    verification_required: bool = False
    verification: dict = field(default_factory=new_verification)
    unconfirmed: list[dict] = field(default_factory=list)
    mutations: list[dict] = field(default_factory=list)
    file_states: dict[str, str] = field(default_factory=dict)
    task_policy: dict = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    @property
    def path(self) -> Path:
        return self.store.path(self.id)

    @classmethod
    def create(cls, store, workspace_root, *, session_id=None):
        return cls(
            store=store,
            id=session_id or uuid.uuid4().hex[:16],
            workspace_root=str(Path(workspace_root).resolve()),
        )

    def save(self):
        return self.store.save(self)

    def append_user(self, content: str):
        self.request_start = len(self.history)
        self.history.append({"kind": "user", "content": str(content)})

    def append_feedback(self, content: str):
        self.history.append({"kind": "feedback", "content": str(content)})

    def begin_tool_turn(self, calls, redact=lambda value: value) -> tuple[int, dict]:
        entry = {
            "kind": "tool_turn",
            "calls": [
                {
                    "name": call.name,
                    "args": redact(dict(call.args)),
                    "call_id": call.call_id,
                }
                for call in calls
            ],
            "phases": {call.call_id: "pending" for call in calls},
            "results": {},
        }
        self.history.append(entry)
        return len(self.history) - 1, entry

    @staticmethod
    def start_tool(entry, call_id: str):
        entry["phases"][call_id] = "running"

    @staticmethod
    def finish_tool(entry, call_id: str, result: dict):
        entry["results"][call_id] = dict(result)
        entry["phases"][call_id] = "finished"

    def append_final(self, content: str):
        self.history.append({"kind": "assistant", "content": str(content)})

    def add_unconfirmed(self, operation_id: str, tool: str, paths=()):
        if any(item["id"] == operation_id for item in self.unconfirmed):
            return
        if isinstance(paths, str):
            paths = (paths,)
        self.unconfirmed.append(
            {
                "id": str(operation_id),
                "tool": str(tool),
                "paths": list(paths),
                "observed": False,
            }
        )
        self.verification_required = True

    def recover(self) -> int:
        """Close incomplete calls without replaying their operations."""

        from .contracts import FailureInfo, ToolOutcome
        from .execution import ExecutionContext
        from .mutations import file_revision

        recovered = 0
        read_tools = {"list_files", "read_file", "read_artifact", "search"}
        for index, entry in enumerate(self.history):
            if entry.get("kind") != "tool_turn":
                continue
            calls = {call["call_id"]: call for call in entry["calls"]}
            for call_id, phase in list(entry["phases"].items()):
                if phase == "finished":
                    continue
                call = calls[call_id]
                started = phase == "running"
                effect = "unknown" if started and call["name"] not in read_tools | {"delegate"} else "none"
                data = {}
                receipts = [
                    item
                    for item in self.mutations
                    if item["id"] == f"{index}:{call_id}"
                ]
                if started and receipts:
                    observations = []
                    for receipt in receipts:
                        target = Path(self.workspace_root) / receipt["path"]
                        try:
                            if target.resolve() != target:
                                raise ValueError("file target was redirected")
                            actual = file_revision(
                                target,
                                execution_context=ExecutionContext.standalone(max_seconds=5),
                            )
                        except (OSError, RuntimeError, ValueError):
                            actual = "unknown"
                        state = (
                            "before"
                            if actual == receipt["before_revision"]
                            else (
                                "after"
                                if receipt["after_revision"]
                                and actual == receipt["after_revision"]
                                else "unknown"
                            )
                        )
                        receipt["status"] = {
                            "before": "not_applied",
                            "after": "applied",
                            "unknown": "unknown",
                        }[state]
                        observations.append(
                            {
                                "path": receipt["path"],
                                "actual_revision": actual,
                                "state": state,
                                "receipt": receipt,
                            }
                        )
                    states = {item["state"] for item in observations}
                    effect = (
                        "none"
                        if states == {"before"}
                        else ("changed" if states == {"after"} else "unknown")
                    )
                    data = {"observations": observations}
                affected = tuple(
                    item["path"] for item in data.get("observations", ())
                    if item["state"] != "before"
                )
                for item in data.get("observations", ()):
                    if item["state"] == "after":
                        self.file_states[item["path"]] = item["actual_revision"]
                result = ToolOutcome(
                    call_id,
                    call["name"],
                    "partial_success" if effect in {"changed", "unknown"} else "error",
                    "failed" if started else "not_started",
                    effect,
                    (
                        "Execution was interrupted; inspect current state before retrying."
                        if started
                        else "The operation was persisted but never started."
                    ),
                    structured=data,
                    failure=FailureInfo(
                        "interrupted" if started else "not_started",
                        "old operations are never replayed automatically",
                        "retry_after_change" if started else "no_retry",
                    ),
                    affected_paths=affected,
                    effect_scope="workspace" if effect != "none" else "none",
                ).to_dict()
                self.finish_tool(entry, call_id, result)
                if effect == "unknown":
                    self.add_unconfirmed(
                        f"tool:{index}:{call_id}",
                        call["name"],
                        [item["path"] for item in data.get("observations", ())]
                        or call["args"].get("path", ()),
                    )
                elif effect == "changed":
                    self.verification_required = True
                recovered += 1
        if self.verification.get("status") == "running":
            operation_id = self.verification.get("operation_id") or "verification"
            self.verification.update(status="interrupted", workspace_state=None)
            self.add_unconfirmed(operation_id, "verify")
            recovered += 1
        return recovered

    def current_user_text(self) -> str:
        if not self.history:
            return ""
        entry = self.history[self.request_start]
        return str(entry.get("content", "")) if entry.get("kind") == "user" else ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_root": self.workspace_root,
            "history": self.history,
            "summary": self.summary,
            "covered": self.covered,
            "observed": self.observed,
            "request_start": self.request_start,
            "run": self.run,
            "loop_control": self.loop_control,
            "verification_required": self.verification_required,
            "verification": self.verification,
            "unconfirmed": self.unconfirmed,
            "mutations": self.mutations,
            "file_states": self.file_states,
            "task_policy": self.task_policy,
            "created_at": self.created_at,
        }
