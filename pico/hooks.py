"""Explicit, non-bypassable runtime policy hooks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HookDirective:
    block: bool = False
    stop: bool = False
    reason: str = ""
    guidance: str = ""

    @property
    def active(self):
        return self.block or self.stop or bool(self.reason or self.guidance)


@dataclass(frozen=True)
class BeforeToolContext:
    call: object
    run_id: str
    task_id: str


@dataclass(frozen=True)
class AfterToolContext:
    outcome: object
    tool_steps: int
    run_id: str
    task_id: str


@dataclass(frozen=True)
class TurnContext:
    action_kind: str
    tool_steps: int
    attempts: int
    run_id: str
    task_id: str


class HookRunner:
    def __init__(self, hooks=()):
        self.hooks = tuple(hooks or ())

    @staticmethod
    def _normalize(value):
        if value is None:
            return HookDirective()
        if isinstance(value, HookDirective):
            return value
        if isinstance(value, bool):
            return HookDirective(stop=value)
        raise TypeError("runtime hook must return HookDirective, bool, or None")

    @staticmethod
    def _merge(directives):
        directives = tuple(item for item in directives if item.active)
        return HookDirective(
            block=any(item.block for item in directives),
            stop=any(item.stop for item in directives),
            reason=" | ".join(item.reason for item in directives if item.reason),
            guidance="\n".join(item.guidance for item in directives if item.guidance),
        )

    def _call(self, method_name, context, *, fail_closed):
        decisions = []
        for hook in self.hooks:
            callback = getattr(hook, method_name, None)
            if not callable(callback):
                continue
            try:
                decisions.append(self._normalize(callback(context)))
            except Exception as exc:  # noqa: BLE001 - hook boundary is fail-closed
                decisions.append(
                    HookDirective(
                        block=fail_closed,
                        stop=True,
                        reason=f"{method_name} hook failed: {type(exc).__name__}: {exc}",
                    )
                )
        return self._merge(decisions)

    def before_tool_call(self, context):
        return self._call("before_tool_call", context, fail_closed=True)

    def after_tool_result(self, context):
        return self._call("after_tool_result", context, fail_closed=False)

    def should_stop_after_turn(self, context):
        return self._call("should_stop_after_turn", context, fail_closed=False)
