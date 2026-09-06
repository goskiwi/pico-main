"""Text rendering and token-budget helpers for PromptBuilder."""

from __future__ import annotations

import json
from html import escape

import tiktoken

DEFAULT_SECTION_CAPS = {
    "workspace": 600,
    "repo_map": 1200,
    "working_state": 300,
}
FIXED_SECTION_ALLOCATION_ORDER = (
    "workspace",
    "repo_map",
    "working_state",
)
CONTEXT_ALLOCATION_ORDER = (
    *FIXED_SECTION_ALLOCATION_ORDER,
    "history",
)
CONTEXT_WIRE_ORDER = (
    "workspace",
    "repo_map",
    "history",
    "working_state",
)


class ContextBudgetExceeded(RuntimeError):
    pass


class Tokenizer:
    def __init__(self, model=""):
        try:
            self.encoding = tiktoken.encoding_for_model(str(model or ""))
        except KeyError:
            self.encoding = tiktoken.get_encoding("o200k_base")

    def count(self, text):
        return len(self.encoding.encode(str(text or ""), disallowed_special=()))


def _render_context(raw, available, *, section_caps, count_tokens, history):
    rendered = {}
    budgets = {}
    clipped = []
    history_metadata = None

    def fit(section, text, budget):
        if section == "history":
            return _bounded_history(
                text,
                budget,
                history=history,
                token_counter=_history_token_counter(
                    raw, rendered, count_tokens=count_tokens
                ),
            )
        return (
            _clip_complete_lines(
                text,
                budget,
                token_counter=_context_section_token_counter(
                    raw, rendered, section, count_tokens=count_tokens
                ),
            ),
            None,
        )

    for section in CONTEXT_ALLOCATION_ORDER:
        text = raw[section]
        if not text:
            continue
        remaining = max(0, available - count_tokens(_assemble_input(raw, rendered)))
        budget = remaining if section == "history" else section_caps[section]
        value, selected_metadata = fit(section, text, budget)
        if selected_metadata is not None:
            history_metadata = selected_metadata
        if not value:
            clipped.append(section)
            continue
        candidate = {**rendered, section: value}
        if count_tokens(_assemble_input(raw, candidate)) > available:
            budget = remaining
            value, selected_metadata = fit(section, text, budget)
            if selected_metadata is not None:
                history_metadata = selected_metadata
            candidate = {**rendered, section: value} if value else rendered
        if value and count_tokens(_assemble_input(raw, candidate)) <= available:
            rendered[section] = value
            budgets[section] = budget
            if value != text:
                clipped.append(section)
        else:
            clipped.append(section)

    return (
        rendered,
        budgets,
        {
            "strategy": "fixed_caps_history_remainder",
            "available_input_tokens": available,
            "history_budget_tokens": budgets.get("history", 0),
            "unused_input_tokens": max(
                0, available - count_tokens(_assemble_input(raw, rendered))
            ),
            "clipped_sections": list(dict.fromkeys(clipped)),
        },
        history_metadata,
    )


def _fixed_context(raw, *, section_caps, count_tokens):
    rendered = {}
    for section in FIXED_SECTION_ALLOCATION_ORDER:
        if not raw[section]:
            continue
        value = _clip_complete_lines(
            raw[section],
            section_caps[section],
            token_counter=_context_section_token_counter(
                raw, rendered, section, count_tokens=count_tokens
            ),
        )
        if value:
            rendered[section] = value
    return rendered


def _clip_complete_lines(text, limit, *, token_counter):
    text = str(text).strip()
    limit = max(0, int(limit))
    if not text or limit <= 0:
        return ""
    if token_counter(text) <= limit:
        return text
    marker = "[section truncated at a complete line]"
    if token_counter(marker) > limit:
        return ""
    selected = []
    for line in text.splitlines():
        candidate = "\n".join([*selected, line, marker])
        if token_counter(candidate) > limit:
            break
        selected.append(line)
    return "\n".join([*selected, marker])


def _bounded_history(text, limit, *, history, token_counter):
    text = str(text).strip()
    limit = max(0, int(limit))
    if not text or limit <= 0:
        return "", None
    if token_counter(text) <= limit:
        return text, None
    if history is None:
        return "", None
    bounded, metadata = history.render_recent_projection(
        retain_tokens=limit,
        token_counter=token_counter,
    )
    if token_counter(bounded) > limit:
        return "", None
    return bounded, metadata


def _untrusted_envelope(context):
    lines = ['<untrusted_context trust="untrusted_data">']
    for section in CONTEXT_WIRE_ORDER:
        value = str(context.get(section, "")).strip()
        if not value:
            continue
        lines.extend(
            (
                f'<section name="{section}">',
                escape(value, quote=False),
                "</section>",
            )
        )
    lines.append("</untrusted_context>")
    return "\n".join(lines)


def _context_section_token_counter(raw, context, section, *, count_tokens):
    base_context = {key: value for key, value in context.items() if key != section}
    base_tokens = count_tokens(_assemble_input(raw, base_context))

    def count(text):
        candidate = {**base_context, section: str(text)}
        return max(
            0,
            count_tokens(_assemble_input(raw, candidate)) - base_tokens,
        )

    return count


def _history_token_counter(raw, context, *, count_tokens):
    return _context_section_token_counter(
        {**raw, "history": ""}, context, "history", count_tokens=count_tokens
    )


def _assemble_input(raw, context):
    parts = [raw["runtime_policy"]]
    if raw["repository_instructions"]:
        parts.append(raw["repository_instructions"])
    parts.append(raw["task_request"])
    if context:
        parts.append(_untrusted_envelope(context))
    if raw["latest_user_request"]:
        parts.append(raw["latest_user_request"])
    return "\n\n".join(parts)


def render_runtime_policy(contract, mode, paths):
    if contract is None:
        policy = {
            "mode": "unavailable",
            "verify_changes": False,
            "write_scope": {"mode": "unavailable"},
        }
    else:
        if mode == "ask":
            write_scope = {"mode": "none"}
        elif paths is None:
            write_scope = {"mode": "workspace"}
        else:
            write_scope = {"mode": "paths", "paths": list(paths)}
        policy = {
            "mode": mode,
            "verify_changes": contract.verify_changes,
            "write_scope": write_scope,
        }
    return "runtime_policy:\n" + json.dumps(
        policy,
        ensure_ascii=False,
        sort_keys=True,
    )


def render_repository_instructions(instructions):
    if not instructions:
        return ""
    lines = ["<repository_instructions>"]
    for path, content in instructions.items():
        lines.extend(
            (
                f'<instructions path="{escape(path, quote=True)}">',
                escape(content, quote=False),
                "</instructions>",
            )
        )
    lines.append("</repository_instructions>")
    return "\n".join(lines)


def render_working_state(working):
    lines = []
    for label, values in (
        ("constraints", working.constraints),
        ("decisions", working.decisions),
        ("next_steps", working.next_steps),
    ):
        if values:
            lines.append(label + ":")
            lines.extend(f"- {value}" for value in values)
    return "\n".join(lines)


def render_history(history):
    """Return the selected history text and its diagnostics together."""
    if history is None:
        return "", {
            "active_count": 0,
            "selected_count": 0,
            "omitted_count": 0,
            "artifact_references": 0,
        }
    text, metadata = history.render_projection()
    return (text if metadata.get("selected_count", 0) else ""), metadata


def _history_budget(raw, available, *, section_caps, count_tokens):
    empty_history = {**raw, "history": ""}
    fixed_context = _fixed_context(
        raw, section_caps=section_caps, count_tokens=count_tokens
    )
    minimum = _assemble_input(empty_history, fixed_context)
    return max(0, available - count_tokens(minimum))
