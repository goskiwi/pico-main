"""Shared tokenizer-aware text-budget primitives for prompt rendering."""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

import tiktoken


DEFAULT_TOKENIZER_ENCODING = "o200k_base"


@lru_cache(maxsize=32)
def _encoding_for_model(model: str):
    """Resolve the real tiktoken encoding for an OpenAI model name.

    OpenAI-compatible gateways sometimes expose an alias that tiktoken does
    not know.  Those aliases still use a real tokenizer, not a character
    heuristic: Pico falls back to the explicit ``o200k_base`` encoding and
    records that fallback in prompt metadata.
    """
    try:
        return tiktoken.encoding_for_model(str(model or "")), True
    except KeyError:
        return tiktoken.get_encoding(DEFAULT_TOKENIZER_ENCODING), False


def tokenizer_details(model: str = "") -> dict:
    encoding, model_mapping_known = _encoding_for_model(str(model or ""))
    return {
        "model": str(model or ""),
        "encoding": encoding.name,
        "model_mapping_known": model_mapping_known,
    }


def count_tokens(text, *, model: str = ""):
    """Count tokens with tiktoken instead of approximating from characters."""
    value = str(text or "")
    if not value:
        return 0
    encoding, _ = _encoding_for_model(str(model or ""))
    return len(encoding.encode(value, disallowed_special=()))


def _semantic_cut(text, limit):
    """在语义边界截断文本，优先保留完整的信息单元。

    截断优先级：段落 > 句子 > 词 > 字符。
    同等 token 预算下，在语义边界截断比任意位置截断
    能保留更多可理解的上下文。
    """
    if len(text) <= limit:
        return text
    if limit <= 5:
        return text[:limit]

    marker = " ..."
    effective = limit - len(marker)
    if effective <= 0:
        return text[:limit]

    # 段落边界：\n\n 或 \n
    for sep in ("\n\n", "\n"):
        pos = text.rfind(sep, 0, effective + 1)
        if pos > effective // 3:
            return text[:pos].rstrip() + marker

    # 句子边界：. ! ? （后跟空格或结尾）
    match = None
    for match in re.finditer(r"[.!?](?:\s|$)", text[:effective + 1]):
        pass
    if match and match.end() > effective // 3:
        return text[: match.end()].rstrip() + marker

    # 词边界
    pos = text.rfind(" ", 0, effective + 1)
    if pos > effective // 3:
        return text[:pos].rstrip() + marker

    # 兜底：硬截
    return text[:effective] + marker


def _token_clip(text, token_budget, *, token_counter: Callable[[str], int] = count_tokens):
    text = str(text)
    token_budget = int(token_budget or 0)
    if token_budget <= 0:
        return ""
    if token_counter(text) <= token_budget:
        return text
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _semantic_cut(text, mid)
        if token_counter(candidate) <= token_budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None
    token_counter: Callable[[str], int] = count_tokens

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)

    @property
    def raw_tokens(self):
        return self.token_counter(self.raw)

    @property
    def rendered_tokens(self):
        return self.token_counter(self.rendered)

    @property
    def budget_tokens(self):
        return max(0, int(self.budget or 0))
