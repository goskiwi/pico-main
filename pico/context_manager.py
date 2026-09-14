"""Token counting and bounded text helpers for model context."""

import tiktoken


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


def clip_complete_lines(text, limit, *, token_counter):
    text = str(text).strip()
    limit = max(0, int(limit))
    if not text or limit <= 0:
        return ""
    if token_counter(text) <= limit:
        return text
    marker = "[truncated at a complete line]"
    if token_counter(marker) > limit:
        return ""
    selected = []
    for line in text.splitlines():
        candidate = "\n".join([*selected, line, marker])
        if token_counter(candidate) > limit:
            break
        selected.append(line)
    return "\n".join([*selected, marker])
