"""Normalized configuration for one Pico runtime."""

from __future__ import annotations

from dataclasses import dataclass

from .workspace import normalize_relative_file


def _allowed_tools(value):
    if value is None:
        return None
    normalized = tuple(str(name).strip() for name in value)
    if not normalized or any(not name for name in normalized):
        raise ValueError("allowed_tools must be a non-empty sequence of tool names")
    return normalized


def _allowed_write_paths(value):
    if value is None:
        return None
    normalized = tuple(normalize_relative_file(path) for path in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("allowed_write_paths must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class PicoConfig:
    mode: str = "code"
    max_agent_turns: int = 32
    max_new_tokens: int = 32000
    allowed_tools: tuple[str, ...] | None = None
    turn_timeout_seconds: int = 600
    context_budget_tokens: int = 272000
    model_context_window_tokens: int | None = None
    compaction_reserve_tokens: int = 32000
    compaction_keep_recent_tokens: int = 20000
    summary_max_output_tokens: int = 16000
    verification_command: str = ""
    verification_required: bool | None = None
    allowed_write_paths: tuple[str, ...] | None = None

    def __post_init__(self):
        if self.mode not in {"ask", "code", "auto"}:
            raise ValueError("mode must be ask, code, or auto")
        if not isinstance(self.verification_command, str):
            raise TypeError("verification_command must be a string")
        if self.verification_required is not None and not isinstance(
            self.verification_required, bool
        ):
            raise TypeError("verification_required must be a boolean or None")
        values = {
            "max_agent_turns": int(self.max_agent_turns),
            "max_new_tokens": int(self.max_new_tokens),
            "turn_timeout_seconds": int(self.turn_timeout_seconds),
            "context_budget_tokens": int(self.context_budget_tokens),
            "compaction_reserve_tokens": int(self.compaction_reserve_tokens),
            "compaction_keep_recent_tokens": int(self.compaction_keep_recent_tokens),
            "summary_max_output_tokens": int(self.summary_max_output_tokens),
        }
        if any(values[name] < 1 for name in (
            "max_agent_turns", "max_new_tokens", "turn_timeout_seconds",
            "summary_max_output_tokens",
        )):
            raise ValueError("runtime limits must be positive")
        if values["context_budget_tokens"] <= values["max_new_tokens"]:
            raise ValueError("context budget must exceed max_new_tokens")
        model_window = self.model_context_window_tokens
        if model_window is not None:
            model_window = int(model_window)
            if model_window < 1:
                raise ValueError("model context window must be positive")
            if values["context_budget_tokens"] > model_window:
                raise ValueError("context budget must not exceed the configured model context window")
        if values["compaction_reserve_tokens"] < values["max_new_tokens"]:
            raise ValueError("compaction reserve must be at least max_new_tokens")
        if values["compaction_reserve_tokens"] >= values["context_budget_tokens"]:
            raise ValueError("compaction reserve must be smaller than the context budget")
        available = values["context_budget_tokens"] - values["compaction_reserve_tokens"]
        if not 1 <= values["compaction_keep_recent_tokens"] <= available:
            raise ValueError("compaction keep_recent must fit below the compaction threshold")
        normalized = {
            "mode": str(self.mode),
            "model_context_window_tokens": model_window,
            **values,
            "allowed_tools": _allowed_tools(self.allowed_tools),
            "verification_command": self.verification_command,
            "verification_required": self.verification_required,
            "allowed_write_paths": _allowed_write_paths(self.allowed_write_paths),
        }
        for name, value in normalized.items():
            object.__setattr__(self, name, value)
