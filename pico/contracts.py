"""Small, provider-neutral contracts used across the runtime."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

ACTION_KINDS = frozenset({"tool", "final", "invalid"})
TOOL_STATUSES = frozenset({"success", "error", "rejected", "partial_success"})
EXECUTION_STATES = frozenset({"not_started", "completed", "failed"})
SIDE_EFFECT_STATES = frozenset({"none", "changed", "partial", "unknown"})
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


def _validate_effect_facts(side_effect_state, affected_paths, effect_scope):
    if side_effect_state == "none":
        if affected_paths:
            raise ValueError("effect-free outcome cannot contain affected paths")
        if effect_scope != "none":
            raise ValueError("effect-free outcome requires none effect scope")
        return
    if side_effect_state in {"changed", "partial"}:
        if not affected_paths:
            raise ValueError("known side effects require affected paths")
        if effect_scope == "none":
            raise ValueError("known side effects require an effect scope")
        return
    if effect_scope == "none":
        raise ValueError("unknown side effects require an effect scope")


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
    tool_call: ToolCall | None = None
    content: str = ""

    def __post_init__(self):
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unsupported model action: {self.kind}")
        if self.kind == "tool" and self.tool_call is None:
            raise ValueError("tool action requires one tool call")
        if self.kind != "tool" and self.tool_call is not None:
            raise ValueError("only tool actions may contain a tool call")
        if self.tool_call is not None and self.tool_call.name == "submit_final":
            raise ValueError(
                "submit_final is a final action, not an executable tool action"
            )

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
    def invalid(cls, content: str):
        return cls("invalid", content=str(content))


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
    effect_scope: str = "none"
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
        if self.effect_scope not in EFFECT_SCOPES:
            raise ValueError(f"invalid effect scope: {self.effect_scope}")
        if not isinstance(self.artifact_id, str):
            raise TypeError("tool artifact id must be text")
        if self.artifact_id and not TOOL_ARTIFACT_ID.fullmatch(self.artifact_id):
            raise ValueError("invalid tool artifact id")
        if not isinstance(self.structured, dict):
            raise TypeError("tool outcome structured result must be an object")
        if self.status == "success" and self.execution_state != "completed":
            raise ValueError("successful outcome must complete execution")
        if self.status == "rejected" and self.execution_state != "not_started":
            raise ValueError("rejected outcome must not start execution")
        if self.status in {"error", "rejected", "partial_success"} and self.failure is None:
            raise ValueError(f"{self.status} outcome requires failure information")
        if self.status == "success" and self.failure is not None:
            raise ValueError("successful outcome cannot contain failure information")
        _validate_effect_facts(
            self.side_effect_state, self.affected_paths, self.effect_scope
        )

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
            "effect_scope": self.effect_scope,
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
            "effect_scope",
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
            effect_scope=str(value["effect_scope"]),
            artifact_id=value["artifact_id"],
        )

    def model_payload(self):
        payload = {
            "status": self.status,
            "execution_state": self.execution_state,
            "side_effect_state": self.side_effect_state,
        }
        if self.content:
            payload["content"] = self.content
        # Keep recovery transactions in the durable outcome, not in every
        # model response. All other tool-specific diagnostics remain visible.
        structured = {key: value for key, value in self.structured.items()
                      if key != "path_transitions"}
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
        if self.effect_scope != "none":
            payload["effect_scope"] = self.effect_scope
        if self.artifact_id:
            payload["artifact_id"] = self.artifact_id
        return payload

    def render_for_model(self):
        payload = self.model_payload()

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
                "expected_revision", "actual_revision", "start_line", "end_line",
                "total_lines", "offset", "end_offset", "next_offset", "total_bytes",
                "has_more", "truncated", "artifact_id", "exit_code", "stop_reason",
                "output_limited", "status", "external_change_observed",
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
