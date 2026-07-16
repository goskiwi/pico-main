"""Normalized model actions used by the agent control loop."""

from __future__ import annotations

from dataclasses import dataclass, field

from .parser import parse_model_output


ACTION_TOOL = "tool"
ACTION_FINAL = "final"
ACTION_RETRY = "retry"
ACTION_KINDS = frozenset({ACTION_TOOL, ACTION_FINAL, ACTION_RETRY})


@dataclass(frozen=True)
class ModelAction:
    """One validated decision returned by a model backend."""

    kind: str
    name: str = ""
    args: dict = field(default_factory=dict)
    answer: str = ""
    error: str = ""
    protocol: str = "text"
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


def action_from_text(raw, *, require_explicit_final=False, protocol="text"):
    """Normalize a text-protocol model response into ``ModelAction``."""
    kind, payload = parse_model_output(
        raw,
        require_explicit_final=bool(require_explicit_final),
    )
    if kind == ACTION_TOOL:
        return ModelAction.tool(
            payload.get("name", ""),
            payload.get("args", {}),
            protocol=protocol,
            raw_preview=raw,
        )
    if kind == ACTION_FINAL:
        return ModelAction.final(payload, protocol=protocol, raw_preview=raw)
    return ModelAction.retry(payload, protocol=protocol, raw_preview=raw)


def _preview(value, limit=800):
    text = str(value or "").strip()
    return text[: int(limit)]
