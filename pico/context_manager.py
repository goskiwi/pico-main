"""Pure token budgeting and complete-History selection for PromptBuilder."""

from __future__ import annotations

import tiktoken

from .history import HistoryBudgetExceeded

DEFAULT_SECTION_CAPS = {
    "workspace": 600,
}
FIXED_SECTION_ALLOCATION_ORDER = (
    "workspace",
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


def select_context(
    raw,
    available,
    *,
    section_caps,
    count_tokens,
    history,
    render_input,
):
    rendered = required_context(raw)
    if count_tokens(render_input(raw, rendered)) > available:
        raise ContextBudgetExceeded("required Runtime context exceeds the model budget")

    required_history = (
        history.render_required_projection() if history is not None else ""
    )
    if required_history:
        candidate = {**rendered, "history": required_history}
        if count_tokens(render_input(raw, candidate)) > available:
            raise ContextBudgetExceeded(
                "committed compaction summary exceeds the model budget"
            )
        rendered = candidate

    for section in FIXED_SECTION_ALLOCATION_ORDER:
        text = raw[section]
        if not text:
            continue
        remaining = max(0, available - count_tokens(render_input(raw, rendered)))
        budget = section_caps[section]
        token_counter = _context_section_token_counter(
            raw,
            rendered,
            section,
            count_tokens=count_tokens,
            render_input=render_input,
        )
        value = _clip_complete_lines(text, budget, token_counter=token_counter)
        if not value:
            continue
        candidate = {**rendered, section: value}
        if count_tokens(render_input(raw, candidate)) > available:
            budget = remaining
            value = _clip_complete_lines(text, budget, token_counter=token_counter)
            candidate = {**rendered, section: value} if value else rendered
        if value and count_tokens(render_input(raw, candidate)) <= available:
            rendered[section] = value

    if raw["history"]:
        without_history = {
            key: value for key, value in rendered.items() if key != "history"
        }
        remaining = max(
            0,
            available - count_tokens(render_input(raw, without_history)),
        )
        value = _bounded_history(
            raw["history"],
            remaining,
            history=history,
            token_counter=history_token_counter(
                raw,
                without_history,
                count_tokens=count_tokens,
                render_input=render_input,
            ),
        )
        if value:
            candidate = {**without_history, "history": value}
            if count_tokens(render_input(raw, candidate)) <= available:
                rendered = candidate
    return rendered


def fixed_context(raw, *, section_caps, count_tokens, render_input, history=None):
    rendered = required_context(raw)
    required_history = (
        history.render_required_projection() if history is not None else ""
    )
    if required_history:
        rendered["history"] = required_history
    for section in FIXED_SECTION_ALLOCATION_ORDER:
        if not raw[section]:
            continue
        value = _clip_complete_lines(
            raw[section],
            section_caps[section],
            token_counter=_context_section_token_counter(
                raw,
                rendered,
                section,
                count_tokens=count_tokens,
                render_input=render_input,
            ),
        )
        if value:
            rendered[section] = value
    return rendered


def required_context(raw):
    return (
        {"user_messages": raw["user_messages"]}
        if raw.get("user_messages")
        else {}
    )


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
        return ""
    if token_counter(text) <= limit:
        return text
    if history is None:
        return ""
    try:
        compacted = history.render_compacted_projection(
            retain_tokens=limit,
            token_counter=token_counter,
        )
    except HistoryBudgetExceeded as exc:
        raise ContextBudgetExceeded(str(exc)) from exc
    if compacted is not None:
        if token_counter(compacted) > limit:
            return ""
        return compacted
    bounded = history.render_recent_projection(
        retain_tokens=limit,
        token_counter=token_counter,
    )
    if token_counter(bounded) > limit:
        return ""
    return bounded


def _context_section_token_counter(
    raw, context, section, *, count_tokens, render_input
):
    base_context = {key: value for key, value in context.items() if key != section}
    base_tokens = count_tokens(render_input(raw, base_context))

    def count(text):
        candidate = {**base_context, section: str(text)}
        return max(
            0,
            count_tokens(render_input(raw, candidate)) - base_tokens,
        )

    return count


def history_token_counter(raw, context, *, count_tokens, render_input):
    return _context_section_token_counter(
        {**raw, "history": ""},
        context,
        "history",
        count_tokens=count_tokens,
        render_input=render_input,
    )


def history_budget(raw, available, *, fixed_context, count_tokens, render_input):
    empty_history = {**raw, "history": ""}
    minimum = render_input(empty_history, fixed_context)
    return max(0, available - count_tokens(minimum))
