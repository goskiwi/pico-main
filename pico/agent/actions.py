"""Normalized model actions used by the agent control loop."""

from __future__ import annotations

from dataclasses import dataclass, field


ACTION_TOOL = "tool"
ACTION_FINAL = "final"
ACTION_RETRY = "retry"
ACTION_KINDS = frozenset({ACTION_TOOL, ACTION_FINAL, ACTION_RETRY})


@dataclass(frozen=True)
class ModelAction:
    """One validated decision returned by a model backend."""

    kind: str
    protocol: str
    name: str = ""
    args: dict = field(default_factory=dict)
    answer: str = ""
    error: str = ""
    raw_preview: str = ""
    call_id: str = ""

    def __post_init__(self):
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unsupported model action kind: {self.kind}")
        if self.kind == ACTION_TOOL:
            if not str(self.name).strip():
                raise ValueError("tool action requires a name")
            if not isinstance(self.args, dict):
                raise ValueError("tool action args must be an object")
        if self.kind == ACTION_FINAL and not str(self.answer).strip():
            raise ValueError("final action requires a non-empty answer")
        if self.kind == ACTION_RETRY and not str(self.error).strip():
            raise ValueError("retry action requires an error")

    @classmethod
    def tool(cls, name, args, *, protocol, raw_preview="", call_id=""):
        return cls(
            kind=ACTION_TOOL,
            name=str(name).strip(),
            args=dict(args or {}),
            protocol=str(protocol),
            raw_preview=_preview(raw_preview),
            call_id=str(call_id or ""),
        )

    @classmethod
    def final(cls, answer, *, protocol, raw_preview="", call_id=""):
        return cls(
            kind=ACTION_FINAL,
            answer=str(answer).strip(),
            protocol=str(protocol),
            raw_preview=_preview(raw_preview),
            call_id=str(call_id or ""),
        )

    @classmethod
    def retry(cls, error, *, protocol, raw_preview="", call_id=""):
        return cls(
            kind=ACTION_RETRY,
            error=str(error).strip(),
            protocol=str(protocol),
            raw_preview=_preview(raw_preview),
            call_id=str(call_id or ""),
        )

def _preview(value, limit=800):
    text = str(value or "").strip()
    return text[: int(limit)]
