"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import memory as memorylib
from .config import (
    DEFAULT_TOTAL_BUDGET,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_REDUCTION_ORDER,
    HISTORY_RECENT_WINDOW,
    RELEVANT_MEMORY_LIMIT,
    FILE_PRIORITY_LIMIT,
    LLM_COMPACT_MAX_INPUT_CHARS,
    LLM_COMPACT_MAX_OUTPUT_TOKENS,
)


SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
SHELL_IMPORTANT_LINE_PATTERN = re.compile(
    r"(?i)(\b(error|failed|failure|traceback|exception|assert|assertion|timeout)\b|assertionerror|exit_code:\s*[1-9])"
)


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _estimate_tokens(text):
    text = str(text or "")
    if not text:
        return 0
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    other_chars = len(text) - cjk_chars
    # Conservative approximation for mixed repo text: CJK is denser, ASCII code/prose is usually ~4 chars/token.
    return max(1, int((cjk_chars / 1.5) + (other_chars / 4.0) + 0.999))


def _tokenize_for_priority(text):
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_./-]+", str(text or ""))}


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
        return _estimate_tokens("x" * max(0, int(self.budget or 0)))


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        llm_history_compaction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
            llm_history_compaction_enabled = self.agent.feature_enabled("llm_history_compaction")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts["prefix"] = checkpoint_text + "\n\n" + section_texts["prefix"]
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        rendered = self._render_sections(
            section_texts,
            budgets,
            selected_notes=selected_notes,
            llm_history_compaction_enabled=llm_history_compaction_enabled,
        )
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 relevant_memory，再牺牲 history，然后才动 memory 和 prefix。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(
                    section_texts,
                    budgets,
                    selected_notes=selected_notes,
                    llm_history_compaction_enabled=llm_history_compaction_enabled,
                )
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
        )
        return prompt, metadata

    def _render_sections_without_reduction(self, section_texts, selected_notes=None):
        selected_notes = selected_notes or []
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(
                f"- {memorylib.render_relevant_memory_note(note)}"
                for note in selected_notes
                if memorylib.render_relevant_memory_note(note)
            )
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [memorylib.render_relevant_memory_note(note) for note in selected_notes],
                    "rendered_notes": [memorylib.render_relevant_memory_note(note) for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None, llm_history_compaction_enabled=False):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(
                    int(budget or 0),
                    llm_history_compaction_enabled=llm_history_compaction_enabled,
                )
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [
            memorylib.render_relevant_memory_note(note)
            for note in selected_notes
            if str(note.get("text", "")).strip()
        ]
        note_texts = [text for text in note_texts if str(text).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget, llm_history_compaction_enabled=False):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        # history 超预算时，用模型把较旧的 transcript 改写成结构化接续摘要；
        # 最近窗口仍保留原始视图，给下一步决策留下刚发生的工具证据。
        recent_window = HISTORY_RECENT_WINDOW
        recent_start = max(0, len(history) - recent_window)
        older_history = history[:recent_start]
        recent_history = history[recent_start:]
        compact_summary = ""
        compact_error = ""
        compact_used = False
        fallback_details = {
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        if llm_history_compaction_enabled and older_history and len(raw) > budget:
            try:
                compact_summary = self._llm_compact_history(older_history)
                compact_used = bool(compact_summary)
            except Exception as exc:
                compact_error = str(exc)

        if not compact_summary and older_history:
            compact_summary, fallback_details = self._fallback_compact_summary(older_history)

        recent_lines = []
        for item in recent_history:
            recent_lines.extend(self._render_history_item(item, 900))

        rendered = self._render_compacted_transcript(
            compact_summary=compact_summary,
            recent_lines=recent_lines,
            budget=budget,
        )

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered.splitlines()[1:],
                "older_entries_count": len(older_history),
                **fallback_details,
                "llm_compact_used": compact_used,
                "llm_compact_error": compact_error,
                "llm_compact_summary_chars": len(compact_summary),
            },
        )

    def _render_compacted_transcript(self, compact_summary, recent_lines, budget):
        if budget <= 0:
            budget = 1_000_000

        selected_recent = []
        for line in reversed(recent_lines):
            candidate = [line] + selected_recent
            reserved_recent = "\n".join(["Recent transcript:", *candidate])
            fixed = "Transcript:\n"
            if compact_summary:
                fixed += "Session compact summary:\n"
            if len(fixed) + len(reserved_recent) + 1 <= budget:
                selected_recent = candidate
                continue
            if not selected_recent:
                available = max(20, budget - len(fixed) - len("Recent transcript:\n") - 1)
                selected_recent = [_tail_clip(line, available)]
            break

        recent_block = "\n".join(["Recent transcript:", *selected_recent]) if selected_recent else ""
        available_summary = budget - len("Transcript:\n") - len(recent_block)
        if compact_summary:
            available_summary -= len("Session compact summary:\n\n")
        compact_summary = _tail_clip(compact_summary, max(40, available_summary)) if compact_summary else ""

        lines = ["Transcript:"]
        if compact_summary:
            lines.extend(["Session compact summary:", *_indent_block(compact_summary)])
        if recent_block:
            lines.append(recent_block)
        rendered = "\n".join(lines)
        if len(rendered) > budget:
            rendered = _tail_clip(rendered, budget)
        return rendered

    def _llm_compact_history(self, older_history):
        model_client = getattr(self.agent, "model_client", None)
        if model_client is None or not hasattr(model_client, "complete"):
            return ""
        prompt = self._compact_prompt(older_history)
        output = model_client.complete(
            prompt,
            max_new_tokens=LLM_COMPACT_MAX_OUTPUT_TOKENS,
            purpose="history_compact",
        )
        return self._sanitize_compact_output(output)

    def _compact_prompt(self, older_history):
        memory_text = ""
        if hasattr(self.agent, "memory_text"):
            memory_text = str(self.agent.memory_text())
        transcript = _tail_clip(self._raw_history_text(older_history), LLM_COMPACT_MAX_INPUT_CHARS)
        return "\n".join(
            [
                "You are compacting a coding agent transcript.",
                "Respond with TEXT ONLY. Do not call tools. Do not invent facts.",
                "Preserve concrete state needed to continue the task.",
                "",
                "Write exactly these markdown sections:",
                "## Primary Goal",
                "## Current Work",
                "## Files And Code",
                "## Errors And Fixes",
                "## Decisions",
                "## Pending Next Step",
                "",
                "Current memory:",
                memory_text or "- none",
                "",
                "Older transcript to compact:",
                transcript,
            ]
        )

    def _sanitize_compact_output(self, output):
        text = str(output or "").strip()
        text = re.sub(r"</?final>", "", text).strip()
        text = re.sub(r"</?tool[^>]*>", "", text).strip()
        if not text:
            return ""
        return _tail_clip(text, 2400)

    def _fallback_compact_summary(self, older_history):
        lines = []
        seen_reads = set()
        details = {
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }
        for item in older_history:
            if item.get("role") == "tool":
                if item.get("name") == "read_file":
                    path = str(item.get("args", {}).get("path", "")).strip()
                    if path in seen_reads:
                        details["collapsed_duplicate_reads"] += 1
                        continue
                    seen_reads.add(path)
                    summary = self._reusable_file_summary(path)
                    if summary:
                        lines.append(f"- {path} -> {summary}")
                        details["reused_file_summary_count"] += 1
                        continue
                lines.append(f"- {self._summarize_old_tool_item(item)}")
                details["summarized_tool_count"] += 1
            else:
                rendered = self._render_history_item(item, 120)
                lines.extend(f"- {line}" for line in rendered)
        return ("\n".join(lines) if lines else "- none"), details

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            important = [line for line in lines if SHELL_IMPORTANT_LINE_PATTERN.search(line)]
            selected = important[:3] if important else lines[:3]
            summary = " | ".join(selected) if selected else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
                "raw_estimated_tokens": rendered[section].raw_tokens,
                "budget_estimated_tokens": rendered[section].budget_tokens,
                "rendered_estimated_tokens": rendered[section].rendered_tokens,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            "raw_estimated_tokens": rendered[CURRENT_REQUEST_SECTION].raw_tokens,
            "budget_estimated_tokens": None,
            "rendered_estimated_tokens": rendered[CURRENT_REQUEST_SECTION].rendered_tokens,
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_estimated_tokens": _estimate_tokens(prompt),
            "prompt_budget_estimated_tokens": _estimate_tokens("x" * self.total_budget),
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "section_token_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else _estimate_tokens("x" * int(budgets.get(section, 0))))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "file_priority": self._file_priority(user_message),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [memorylib.render_relevant_memory_note(note) for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "raw_estimated_tokens": rendered["relevant_memory"].raw_tokens,
                "rendered_estimated_tokens": rendered["relevant_memory"].rendered_tokens,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
                "llm_compact_used": bool(rendered["history"].details.get("llm_compact_used", False)),
                "llm_compact_error": str(rendered["history"].details.get("llm_compact_error", "")),
                "llm_compact_summary_chars": int(rendered["history"].details.get("llm_compact_summary_chars", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "estimated_tokens": _estimate_tokens(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }

    def _file_priority(self, user_message):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return {"limit": FILE_PRIORITY_LIMIT, "files": []}
        snapshot = memory.to_dict()
        recent_files = [str(path) for path in snapshot.get("working", {}).get("recent_files", []) if str(path).strip()]
        file_summaries = snapshot.get("file_summaries", {}) if isinstance(snapshot.get("file_summaries", {}), dict) else {}
        user_tokens = _tokenize_for_priority(user_message)
        scores = {}

        def add(path, points, reason):
            path = str(path or "").strip()
            if not path:
                return
            item = scores.setdefault(path, {"path": path, "score": 0, "reasons": []})
            item["score"] += int(points)
            if reason not in item["reasons"]:
                item["reasons"].append(reason)

        for index, path in enumerate(recent_files):
            add(path, 20 + index, "recent_memory")
        for path in file_summaries:
            add(path, 8, "has_summary")
        for path in set(recent_files) | set(file_summaries):
            path_tokens = _tokenize_for_priority(path)
            basename_tokens = _tokenize_for_priority(path.rsplit("/", 1)[-1])
            if path in str(user_message) or path_tokens & user_tokens or basename_tokens & user_tokens:
                add(path, 40, "mentioned_in_request")

        history = list(getattr(self.agent, "session", {}).get("history", []))
        for offset, item in enumerate(reversed(history[-12:])):
            if item.get("role") != "tool":
                continue
            name = str(item.get("name", ""))
            args = item.get("args", {}) if isinstance(item.get("args", {}), dict) else {}
            path = args.get("path")
            if not path:
                continue
            add(path, max(1, 12 - offset), "recent_tool")
            if name in {"write_file", "patch_file"}:
                add(path, 25, "recent_write")
            elif name == "read_file":
                add(path, 10, "recent_read")

        ranked = sorted(scores.values(), key=lambda item: (-item["score"], item["path"]))[:FILE_PRIORITY_LIMIT]
        return {"limit": FILE_PRIORITY_LIMIT, "files": ranked}
