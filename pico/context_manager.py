"""Pure token budgeting and complete-History selection for PromptBuilder."""

from __future__ import annotations

import tiktoken

DEFAULT_SECTION_CAPS = {
    "workspace": 600,
}
FIXED_SECTION_ALLOCATION_ORDER = (
    "workspace",
)
CONTEXT_ALLOCATION_ORDER = (
    *FIXED_SECTION_ALLOCATION_ORDER,
    "history",
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
    rendered = _required_context(raw)
    if count_tokens(render_input(raw, rendered)) > available:
        raise ContextBudgetExceeded("required Runtime context exceeds the model budget")

    def fit(section, text, budget):
        if section == "history":
            return _bounded_history(
                text,
                budget,
                history=history,
                token_counter=history_token_counter(
                    raw,
                    rendered,
                    count_tokens=count_tokens,
                    render_input=render_input,
                ),
            )
        return _clip_complete_lines(
            text,
            budget,
            token_counter=_context_section_token_counter(
                raw,
                rendered,
                section,
                count_tokens=count_tokens,
                render_input=render_input,
            ),
        )

    for section in CONTEXT_ALLOCATION_ORDER:
        text = raw[section]
        if not text:
            continue
        remaining = max(0, available - count_tokens(render_input(raw, rendered)))
        budget = remaining if section == "history" else section_caps[section]
        value = fit(section, text, budget)
        if not value:
            continue
        candidate = {**rendered, section: value}
        if count_tokens(render_input(raw, candidate)) > available:
            budget = remaining
            value = fit(section, text, budget)
            candidate = {**rendered, section: value} if value else rendered
        if value and count_tokens(render_input(raw, candidate)) <= available:
            rendered[section] = value
    return rendered


def fixed_context(raw, *, section_caps, count_tokens, render_input):
    rendered = _required_context(raw)
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


def _required_context(raw):
    return {
        key: raw[key]
        for key in ("runtime_evidence",)
        if raw.get(key)
    }


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
    compacted = history.render_compacted_projection(
        retain_tokens=limit,
        token_counter=token_counter,
    )
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
