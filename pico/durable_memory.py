"""Durable memory promotion rules."""

import re
from dataclasses import dataclass

from .config import REDACTED_VALUE
DURABLE_MEMORY_INTENT_PATTERN = re.compile(r"(?i)\b(capture|remember|save|store|persist|note)\b")
DURABLE_MEMORY_INTENT_ZH_PATTERN = re.compile(r"(记住|保存|记录|沉淀|长期记忆|持久记忆)")
DURABLE_MEMORY_LINE_PATTERNS = (
    ("user", re.compile(r"(?i)^User:\s*(.+)$")),
    ("feedback", re.compile(r"(?i)^Feedback:\s*(.+)$")),
    ("project", re.compile(r"(?i)^Project:\s*(.+)$")),
    ("reference", re.compile(r"(?i)^Reference:\s*(.+)$")),
    ("user", re.compile(r"^用户：\s*(.+)$")),
    ("feedback", re.compile(r"^反馈：\s*(.+)$")),
    ("project", re.compile(r"^项目：\s*(.+)$")),
    ("reference", re.compile(r"^参考：\s*(.+)$")),
)
SECRET_SHAPED_TEXT_PATTERN = re.compile(r"(?i)(\b(api[_ -]?key|token|secret|password)\b|sk-[A-Za-z0-9_-]{6,})")
CODE_LOCATION_PATTERN = re.compile(r"(?i)(\b[A-Za-z0-9_./-]+\.(py|js|ts|tsx|jsx|go|rs|java|md):\d+\b|\bline\s+\d+\b)")


@dataclass
class DurablePromotionResult:
    promoted: list[str]
    rejections: list[str]
    superseded: list[str]


def reject_reason(note_text):
    text = str(note_text or "").strip()
    lowered = text.lower()
    if not text:
        return "empty"
    if REDACTED_VALUE in text or SECRET_SHAPED_TEXT_PATTERN.search(text):
        return "secret_shaped"
    checkpoint_like_prefixes = (
        "current goal",
        "current blocker",
        "next step",
        "current phase",
        "key files",
        "freshness",
        "当前目标",
        "当前卡点",
        "下一步",
        "当前阶段",
        "关键文件",
        "已完成",
        "已排除",
    )
    if any(lowered.startswith(prefix) or f" {prefix}" in lowered for prefix in checkpoint_like_prefixes):
        return "transient_task_state"
    if CODE_LOCATION_PATTERN.search(text):
        return "code_location"
    if re.search(r"(?i)\b(stdout|stderr|traceback|exit_code)\b", text) or len(text) > 220:
        return "noisy_output"
    return ""


def extract_promotions(user_message, final_answer):
    user_text = str(user_message or "")
    if not (DURABLE_MEMORY_INTENT_PATTERN.search(user_text) or DURABLE_MEMORY_INTENT_ZH_PATTERN.search(user_text)):
        return [], []
    promotions = []
    rejections = []
    for line in str(final_answer or "").splitlines():
        text = line.strip()
        if not text or REDACTED_VALUE in text:
            continue
        for memory_type, pattern in DURABLE_MEMORY_LINE_PATTERNS:
            match = pattern.match(text)
            if not match:
                continue
            note_text = match.group(1).strip()
            if note_text:
                reason = reject_reason(note_text)
                if reason:
                    rejections.append(f"{memory_type}:{reason}")
                    break
                promotions.append((memory_type, note_text))
            break
    return promotions, rejections


def promote(memory, user_message, final_answer):
    promotions, rejections = extract_promotions(user_message, final_answer)
    promoted, superseded = memory.promote_durable(promotions)
    return DurablePromotionResult(
        promoted=promoted,
        rejections=rejections,
        superseded=superseded,
    )


def promote_candidates(memory, candidates):
    promotions = []
    rejections = []
    seen = set()
    for memory_type, note_text in candidates:
        memory_type = str(memory_type or "").strip()
        note_text = str(note_text or "").strip()
        if memory_type not in {"user", "feedback", "project", "reference"}:
            if memory_type:
                rejections.append(f"{memory_type}:invalid_type")
            continue
        reason = reject_reason(note_text)
        if reason:
            rejections.append(f"{memory_type}:{reason}")
            continue
        key = (memory_type, note_text)
        if key in seen:
            continue
        seen.add(key)
        promotions.append(key)
    promoted, superseded = memory.promote_durable(promotions)
    return DurablePromotionResult(
        promoted=promoted,
        rejections=rejections,
        superseded=superseded,
    )
