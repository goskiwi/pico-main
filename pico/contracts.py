"""Small, provider-neutral contracts used across the runtime."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

ACTION_KINDS = frozenset(
    {"tool", "final", "truncated", "service_failed", "protocol_error"}
)
TOOL_STATUSES = frozenset({"success", "error", "rejected", "partial_success"})
EXECUTION_STATES = frozenset({"not_started", "completed", "failed"})
SIDE_EFFECT_STATES = frozenset({"none", "changed", "partial", "untracked"})
EFFECT_SCOPES = frozenset({"none", "workspace"})
TOOL_ARTIFACT_ID_PATTERN = r"^tool_[a-f0-9]{16}_[a-f0-9]{10}$"
TOOL_ARTIFACT_ID = re.compile(TOOL_ARTIFACT_ID_PATTERN)
TOOL_OUTPUT_MAX_BYTES = 12 * 1024
RECOVERY_CONDITIONS = frozenset(
    {
        "retry_after_change",
        "retry_after_wait",
        "user_action_required",
        "no_retry",
    }
)


def _validate_effect_facts(side_effect_state, affected_paths):
    if side_effect_state == "none":
        if affected_paths:
            raise ValueError("effect-free outcome cannot contain affected paths")
        return
    if side_effect_state in {"changed", "partial"}:
        if not affected_paths:
            raise ValueError("known side effects require affected paths")
        return
    if affected_paths:
        raise ValueError("untracked side effects cannot name affected paths")


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
class ToolExecutionPlan:
    """One immutable pre-effect plan persisted before a Tool Runner starts."""

    effect_scope: str
    paths: tuple[tuple[str, Any], ...] = ()
    operation: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.effect_scope not in EFFECT_SCOPES:
            raise ValueError(f"invalid planned effect scope: {self.effect_scope}")
        if not isinstance(self.operation, dict):
            raise TypeError("tool execution operation must be an object")
        if self.paths and self.effect_scope == "none":
            raise ValueError("planned paths require a non-empty effect scope")
        logical = tuple(str(path) for path, _target in self.paths)
        if any(not path for path in logical) or len(set(logical)) != len(logical):
            raise ValueError("planned effect paths must be non-empty and unique")


@dataclass(frozen=True)
class ToolRunnerResult:
    """Exact result returned by a tool runner before Runtime auditing."""

    content: str
    structured: dict[str, Any] = field(default_factory=dict)
    affected_paths: tuple[str, ...] = ()
    effect_scope: str = "none"
    failure: FailureInfo | None = None

    def __post_init__(self):
        if not isinstance(self.structured, dict):
            raise TypeError("tool runner structured result must be an object")
        if self.effect_scope not in EFFECT_SCOPES:
            raise ValueError(f"invalid tool effect scope: {self.effect_scope}")
        if self.affected_paths and self.effect_scope == "none":
            raise ValueError("affected paths require a non-empty effect scope")


@dataclass(frozen=True)
class ModelAction:
    kind: str
    tool_calls: tuple[ToolCall, ...] = ()
    content: str = ""

    def __post_init__(self):
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unsupported model action: {self.kind}")
        if self.kind == "tool" and not self.tool_calls:
            raise ValueError("tool action requires at least one tool call")
        if self.kind != "tool" and self.tool_calls:
            raise ValueError("only tool actions may contain tool calls")
        if any(call.name == "submit_final" for call in self.tool_calls):
            raise ValueError(
                "submit_final is a final action, not an executable tool action"
            )
        call_ids = tuple(call.call_id for call in self.tool_calls)
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("tool action call ids must be unique")

    @classmethod
    def tool(cls, name: str, args: dict[str, Any], *, call_id: str = ""):
        return cls("tool", tool_calls=(ToolCall(name, args, call_id),))

    @classmethod
    def tools(cls, calls):
        normalized = tuple(calls)
        if len(normalized) < 2:
            raise ValueError("multi-tool action requires at least two calls")
        if any(not isinstance(call, ToolCall) for call in normalized):
            raise TypeError("multi-tool action entries must be ToolCall values")
        return cls("tool", tool_calls=normalized)

    @classmethod
    def final(cls, content: str):
        content = str(content).strip()
        if not content:
            raise ValueError("final action requires non-empty content")
        return cls("final", content=content)

    @classmethod
    def truncated(cls, content: str):
        return cls("truncated", content=str(content))

    @classmethod
    def service_failed(cls, content: str):
        return cls("service_failed", content=str(content))

    @classmethod
    def protocol_error(cls, content: str):
        return cls("protocol_error", content=str(content))

    def to_dict(self):
        return {
            "kind": self.kind,
            "tool_calls": [
                {
                    "name": call.name,
                    "args": dict(call.args),
                    "call_id": call.call_id,
                }
                for call in self.tool_calls
            ],
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {"kind", "tool_calls", "content"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid ModelAction")
        if not isinstance(value["tool_calls"], list):
            raise TypeError("ModelAction tool_calls must be a list")
        return cls(
            kind=str(value["kind"]),
            tool_calls=tuple(
                ToolCall(
                    name=str(call["name"]),
                    args=dict(call["args"]),
                    call_id=str(call["call_id"]),
                )
                for call in value["tool_calls"]
            ),
            content=str(value["content"]),
        )


@dataclass(frozen=True)
class AssistantTurn:
    """Provider-neutral Assistant response accepted by the Runtime."""

    action: ModelAction
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.action, ModelAction):
            raise TypeError("assistant turn requires a ModelAction")
        if not isinstance(self.text, str):
            raise TypeError("assistant text must be text")
        if not isinstance(self.usage, dict):
            raise TypeError("assistant turn usage must be an object")

    def to_dict(self):
        return {
            "action": self.action.to_dict(),
            "text": self.text,
            "usage": dict(self.usage),
        }

    @classmethod
    def from_dict(cls, value):
        expected = {"action", "text", "usage"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid AssistantTurn")
        if not isinstance(value["usage"], dict):
            raise TypeError("AssistantTurn usage must be an object")
        return cls(
            action=ModelAction.from_dict(value["action"]),
            text=str(value["text"]),
            usage=dict(value["usage"]),
        )


@dataclass(frozen=True)
class ModelMessage:
    """One chronological User, Assistant, or Tool message sent to a model."""

    role: str
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""

    def __post_init__(self):
        if self.role not in {"user", "assistant", "tool"}:
            raise ValueError("model message has an invalid role")
        if not isinstance(self.text, str):
            raise TypeError("model message text must be text")
        calls = tuple(self.tool_calls)
        if any(not isinstance(call, ToolCall) for call in calls):
            raise TypeError("assistant tool calls must be ToolCall values")
        object.__setattr__(self, "tool_calls", calls)
        if self.role == "user" and (not self.text or calls or self.tool_call_id):
            raise ValueError("user message requires only text")
        if self.role == "assistant" and self.tool_call_id:
            raise ValueError("assistant message cannot be a Tool Result")
        if self.role == "assistant" and not self.text and not calls:
            raise ValueError("assistant message requires text or Tool Calls")
        if self.role == "tool" and (not self.tool_call_id or calls):
            raise ValueError("tool message requires one Tool Call id")

    @classmethod
    def user(cls, text):
        return cls("user", str(text))

    @classmethod
    def assistant(cls, *, text="", tool_calls=()):
        return cls("assistant", str(text), tuple(tool_calls))

    @classmethod
    def tool(cls, call_id, text):
        return cls("tool", str(text), tool_call_id=str(call_id))

    def to_dict(self):
        return {
            "role": self.role,
            "text": self.text,
            "tool_calls": [
                {
                    "name": call.name,
                    "args": dict(call.args),
                    "call_id": call.call_id,
                }
                for call in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
        }


@dataclass(frozen=True)
class FailureInfo:
    code: str
    detail: str = ""
    recovery: str = "no_retry"

    def __post_init__(self):
        if not self.code:
            raise ValueError("failure information requires a code")
        if self.recovery not in RECOVERY_CONDITIONS:
            raise ValueError(f"invalid failure recovery condition: {self.recovery}")

    def to_dict(self):
        return {
            "code": self.code,
            "detail": self.detail,
            "recovery": self.recovery,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {"code", "detail", "recovery"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid failure information")
        return cls(
            code=str(value["code"]),
            detail=str(value["detail"]),
            recovery=str(value["recovery"]),
        )


class ToolFailureError(RuntimeError):
    """Typed pre-effect failure raised before a Tool commits side effects."""

    def __init__(
        self,
        code,
        detail,
        recovery="retry_after_change",
        *,
        structured=None,
    ):
        self.failure = FailureInfo(str(code), str(detail), str(recovery))
        self.structured = dict(structured or {})
        super().__init__(self.failure.detail)


@dataclass(frozen=True)
class ToolOutcome:
    """Canonical fact returned by every tool admission/execution path."""

    tool_call_id: str
    tool_name: str
    status: str
    execution_state: str
    side_effect_state: str
    content: str
    structured: dict[str, Any] = field(default_factory=dict)
    failure: FailureInfo | None = None
    affected_paths: tuple[str, ...] = ()
    artifact_id: str = ""

    def __post_init__(self):
        if not isinstance(self.tool_call_id, str) or not self.tool_call_id.strip():
            raise ValueError("tool outcome requires a call id")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool outcome requires a tool name")
        if self.status not in TOOL_STATUSES:
            raise ValueError(f"invalid tool status: {self.status}")
        if self.execution_state not in EXECUTION_STATES:
            raise ValueError(f"invalid execution state: {self.execution_state}")
        if self.side_effect_state not in SIDE_EFFECT_STATES:
            raise ValueError(f"invalid side-effect state: {self.side_effect_state}")
        if not isinstance(self.artifact_id, str):
            raise TypeError("tool artifact id must be text")
        if self.artifact_id and not TOOL_ARTIFACT_ID.fullmatch(self.artifact_id):
            raise ValueError("invalid tool artifact id")
        if not isinstance(self.structured, dict):
            raise TypeError("tool outcome structured result must be an object")
        if "path_transitions" in self.structured:
            raise ValueError("path_transitions is not part of ToolOutcome")
        if self.status == "success" and self.execution_state != "completed":
            raise ValueError("successful outcome must complete execution")
        if self.status == "rejected" and self.execution_state != "not_started":
            raise ValueError("rejected outcome must not start execution")
        if self.status in {"error", "rejected", "partial_success"} and self.failure is None:
            raise ValueError(f"{self.status} outcome requires failure information")
        if self.status == "success" and self.failure is not None:
            raise ValueError("successful outcome cannot contain failure information")
        _validate_effect_facts(self.side_effect_state, self.affected_paths)

    def to_dict(self):
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "execution_state": self.execution_state,
            "side_effect_state": self.side_effect_state,
            "content": self.content,
            "structured": dict(self.structured),
            "failure": self.failure.to_dict() if self.failure else None,
            "affected_paths": list(self.affected_paths),
            "artifact_id": self.artifact_id,
        }

    @classmethod
    def from_dict(cls, value):
        expected = {
            "tool_call_id",
            "tool_name",
            "status",
            "execution_state",
            "side_effect_state",
            "content",
            "structured",
            "failure",
            "affected_paths",
            "artifact_id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid ToolOutcome")
        if (
            not isinstance(value["structured"], dict)
            or not isinstance(value["affected_paths"], list)
            or not isinstance(value["artifact_id"], str)
        ):
            raise TypeError("ToolOutcome collection fields have invalid types")
        failure = value["failure"]
        return cls(
            tool_call_id=str(value["tool_call_id"]),
            tool_name=str(value["tool_name"]),
            status=str(value["status"]),
            execution_state=str(value["execution_state"]),
            side_effect_state=str(value["side_effect_state"]),
            content=str(value["content"]),
            structured=dict(value["structured"]),
            failure=FailureInfo.from_dict(failure) if failure is not None else None,
            affected_paths=tuple(str(item) for item in value["affected_paths"]),
            artifact_id=value["artifact_id"],
        )

    def model_payload(self, *, retry_instruction=""):
        payload = {
            "status": self.status,
            "execution_state": self.execution_state,
            "side_effect_state": self.side_effect_state,
        }
        if self.content:
            payload["content"] = self.content
        structured = dict(self.structured)
        if self.tool_name in {"write_file", "edit_file"} and self.status == "success":
            revision = structured.pop("after_revision", None)
            structured.pop("before_revision", None)
            if revision is not None:
                structured["revision"] = revision
        if structured:
            payload["structured"] = structured
        if self.failure is not None:
            payload["failure"] = self.failure.to_dict()
        if self.affected_paths and not (
            self.status == "success" and self.side_effect_state == "changed"
            and self.affected_paths == (structured.get("path"),)
        ):
            payload["affected_paths"] = list(self.affected_paths)
        if self.artifact_id:
            payload["artifact_id"] = self.artifact_id
        if retry_instruction:
            payload["retry_instruction"] = str(retry_instruction)
        return payload

    def render_for_model(self, *, retry_instruction=""):
        payload = self.model_payload(retry_instruction=retry_instruction)

        def encode():
            return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"))

        rendered = encode()
        if len(rendered.encode("utf-8")) <= TOOL_OUTPUT_MAX_BYTES:
            return rendered
        if not self.artifact_id:
            raise ValueError("oversized tool output requires an artifact")
        notice = f"\n[Full result: {self.artifact_id}; use read_artifact.]"
        payload["content"] = notice
        if len(encode().encode("utf-8")) > TOOL_OUTPUT_MAX_BYTES:
            essential = {
                "path", "revision", "before_revision", "after_revision",
                "actual_revision", "start_line", "end_line",
                "offset", "end_offset", "next_offset", "total_bytes",
                "has_more", "truncated", "artifact_id", "exit_code", "stop_reason",
                "output_limited", "status",
                "stdout_discarded_bytes", "stderr_discarded_bytes",
            }
            payload["structured"] = {
                key: value for key, value in payload.get("structured", {}).items()
                if key in essential
            }
            payload["metadata_omitted"] = True
            if self.failure is not None:
                payload["failure"] = {**self.failure.to_dict(), "detail": "See artifact for full failure details."}
            if len(encode().encode("utf-8")) > TOOL_OUTPUT_MAX_BYTES:
                raise ValueError(
                    f"essential tool result metadata exceeds output budget; full result: {self.artifact_id}"
                )
        low, high = 0, len(self.content)
        while low < high:
            middle = (low + high + 1) // 2
            payload["content"] = self.content[:middle] + notice
            if len(encode().encode("utf-8")) <= TOOL_OUTPUT_MAX_BYTES:
                low = middle
            else:
                high = middle - 1
        payload["content"] = self.content[:low] + notice
        return encode()
