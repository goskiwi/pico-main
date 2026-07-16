"""Shared text-budget primitives for prompt context rendering."""

import re
from dataclasses import dataclass


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


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


def _token_clip(text, token_budget):
    text = str(text)
    token_budget = int(token_budget or 0)
    if token_budget <= 0:
        return ""
    if _estimate_tokens(text) <= token_budget:
        return text
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _semantic_cut(text, mid)
        if _estimate_tokens(candidate) <= token_budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _estimate_tokens(text):
    text = str(text or "")
    if not text:
        return 0
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other_chars = len(text) - cjk_chars
    # Conservative approximation for mixed repo text: CJK is denser, ASCII code/prose is usually ~4 chars/token.
    return max(1, int((cjk_chars / 1.5) + (other_chars / 4.0) + 0.999))


def _indent_block(text, prefix="  "):
    lines = str(text or "").splitlines()
    if not lines:
        return [prefix + "- none"]
    return [prefix + line for line in lines]


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)

    @property
    def raw_tokens(self):
        return _estimate_tokens(self.raw)

    @property
    def rendered_tokens(self):
        return _estimate_tokens(self.rendered)

    @property
    def budget_tokens(self):
        return max(0, int(self.budget or 0))
