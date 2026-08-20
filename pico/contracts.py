"""Small, provider-neutral contracts used across the runtime."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

ACTION_KINDS = frozenset({"tool", "final", "retry"})
TOOL_STATUSES = frozenset({"ok", "error", "rejected", "partial_success"})
EXECUTION_STATES = frozenset({"not_started", "completed", "failed"})
SIDE_EFFECT_STATES = frozenset({"none", "changed", "partial", "unknown"})
RECOVERY_ACTIONS = frozenset({"continue", "retry", "repair", "replan", "stop"})
EFFECT_SCOPES = frozenset({"none", "workspace", "project_memory", "mixed"})


def canonical_fingerprint(name: str, args: dict[str, Any]) -> str:
    payload = json.dumps(
        {"name": str(name), "args": dict(args)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    call_id: str = ""

    def __post_init__(self):
        if not self.name.strip():
            raise ValueError("tool call requires a name")
        if not isinstance(self.args, dict):
            raise TypeError("tool call args must be an object")
        if not self.call_id:
            object.__setattr__(self, "call_id", "call_" + uuid.uuid4().hex[:12])


@dataclass(frozen=True)
class ToolExecution:
    """Exact result returned by a tool runner before Runtime auditing."""

    content: str
    affected_paths: tuple[str, ...] = ()
    diff_summary: tuple[str, ...] = ()
    effect_scope: str = "none"

    def __post_init__(self):
        if self.effect_scope not in EFFECT_SCOPES:
            raise ValueError(f"invalid tool effect scope: {self.effect_scope}")
        if self.affected_paths and self.effect_scope == "none":
            raise ValueError("affected paths require a non-empty effect scope")


@dataclass(frozen=True)
class ModelAction:
    kind: str
    tool_call: ToolCall | None = None
    content: str = ""
    error: str = ""

    def __post_init__(self):
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unsupported model action: {self.kind}")
        if self.kind == "tool" and self.tool_call is None:
            raise ValueError("tool action requires a tool call")
        if self.kind != "tool" and self.tool_call is not None:
            raise ValueError("only tool actions may contain a tool call")

    @classmethod
    def tool(cls, name: str, args: dict[str, Any], *, call_id: str = ""):
        return cls("tool", tool_call=ToolCall(name, args, call_id))

    @classmethod
    def final(cls, content: str):
        content = str(content).strip()
        if not content:
            raise ValueError("final action requires non-empty content")
        return cls("final", content=content)

    @classmethod
    def retry(cls, content: str, *, error: str = "invalid_model_action"):
        return cls("retry", content=str(content), error=str(error))


@dataclass(frozen=True)
class FailureInfo:
    code: str
    category: str
    detail: str = ""
    retryable: bool = False

    def to_dict(self):
        return {
            "code": self.code,
            "category": self.category,
            "detail": self.detail,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class RecoveryAssessment:
    action: str
    reason: str
    retryability: str
    occurrence: int = 1
    guidance: tuple[str, ...] = ()

    def __post_init__(self):
        if self.action not in RECOVERY_ACTIONS:
            raise ValueError(f"invalid recovery action: {self.action}")

    def to_dict(self):
        return {
            "action": self.action,
            "reason": self.reason,
            "retryability": self.retryability,
            "occurrence": self.occurrence,
            "guidance": list(self.guidance),
        }


@dataclass(frozen=True)
class ToolAttempt:
    attempt: int
    status: str
    execution_state: str
    side_effect_state: str
    duration_ms: int
    affected_paths: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "attempt": self.attempt,
            "status": self.status,
            "execution_state": self.execution_state,
            "side_effect_state": self.side_effect_state,
            "duration_ms": self.duration_ms,
            "affected_paths": list(self.affected_paths),
        }


@dataclass(frozen=True)
class ToolOutcome:
    """Canonical fact returned by every tool admission/execution path."""

    tool_call_id: str
    tool_name: str
    status: str
    execution_state: str
    side_effect_state: str
    content: str
    call_fingerprint: str
    admission: dict[str, Any]
    failure: FailureInfo | None = None
    recovery: RecoveryAssessment | None = None
    attempts: tuple[ToolAttempt, ...] = ()
    affected_paths: tuple[str, ...] = ()
    diff_summary: tuple[str, ...] = ()
    workspace_fingerprint: str = ""
    duration_ms: int = 0
    artifact_id: str = ""
    artifact: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"invalid tool status: {self.status}")
        if self.execution_state not in EXECUTION_STATES:
            raise ValueError(f"invalid execution state: {self.execution_state}")
        if self.side_effect_state not in SIDE_EFFECT_STATES:
            raise ValueError(f"invalid side-effect state: {self.side_effect_state}")
        if self.status in {"error", "rejected", "partial_success"} and self.failure is None:
            raise ValueError(f"{self.status} outcome requires failure information")
        if self.status == "ok" and self.failure is not None:
            raise ValueError("ok outcome cannot contain failure information")

    @property
    def workspace_changed(self):
        return (
            self.side_effect_state in {"changed", "partial"}
            and self.metadata.get("effect_scope", "workspace") in {"workspace", "mixed"}
        )

    def to_dict(self):
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "execution_state": self.execution_state,
            "side_effect_state": self.side_effect_state,
            "content": self.content,
            "call_fingerprint": self.call_fingerprint,
            "admission": dict(self.admission),
            "failure": self.failure.to_dict() if self.failure else None,
            "recovery": self.recovery.to_dict() if self.recovery else None,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "affected_paths": list(self.affected_paths),
            "diff_summary": list(self.diff_summary),
            "workspace_fingerprint": self.workspace_fingerprint,
            "duration_ms": self.duration_ms,
            "artifact_id": self.artifact_id,
            "artifact": dict(self.artifact),
            "workspace_changed": self.workspace_changed,
            "metadata": dict(self.metadata),
        }
