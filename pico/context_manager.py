"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

from . import memory as memorylib
from . import skills as skillslib
from .config import (
    DEFAULT_TOTAL_BUDGET,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_REDUCTION_ORDER,
    RELEVANT_MEMORY_LIMIT,
)
from .context_history import HistoryRenderer
from .context_types import (
    SectionRender,
    _estimate_tokens,
    _token_clip,
)


SECTION_ORDER = ("prefix", "memory", "skills", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RECENT_RUN_GUIDANCE = "Use read_file on task_graph, then read_tool_output for node refs."


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
        self.history_renderer = HistoryRenderer(agent)
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
        memory_enabled = self.agent.feature_enabled("memory")
        relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
        context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        llm_history_compaction_enabled = self.agent.feature_enabled("llm_history_compaction")
        dynamic_budget_enabled = self.agent.feature_enabled("dynamic_budget")
        cross_section_dedup_enabled = self.agent.feature_enabled("cross_section_dedup")
        selected_notes = []
        if memory_enabled and relevant_memory_enabled:
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)
        selected_recent_runs = self._recent_runs_for_request(user_message)
        selected_skills = self.agent.select_skills(user_message)
        prefix_text = str(self.agent.prefix)
        checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            prefix_text = checkpoint_text + "\n\n" + prefix_text
        section_texts = {
            "prefix": prefix_text,
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "skills": "",
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        section_texts["skills"] = skillslib.render_skills(selected_skills)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes, recent_runs=selected_recent_runs)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                selected_recent_runs=selected_recent_runs,
                selected_skills=selected_skills,
                user_message=user_message,
                section_texts=section_texts,
                dynamic_adjustment={},
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        dynamic_adjustment = {}
        if dynamic_budget_enabled:
            budgets, dynamic_adjustment = self._dynamic_budget_adjust(budgets, user_message)
        dedup_file_paths = set()
        if cross_section_dedup_enabled and memory_enabled:
            memory_state = self.agent.memory.to_dict()
            dedup_file_paths = set(memory_state.get("file_summaries", {}).keys())
        rendered = self._render_sections(
            section_texts,
            budgets,
            selected_notes=selected_notes,
            recent_runs=selected_recent_runs,
            llm_history_compaction_enabled=llm_history_compaction_enabled,
            dedup_file_paths=dedup_file_paths,
        )
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超过 token 预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 relevant_memory，再牺牲 history，然后才动 memory 和 prefix。
        # 优先压缩旧上下文；仍然超预算时，保留当前请求的首尾并截断。
        prompt_tokens = _estimate_tokens(prompt)
        while prompt_tokens > self.total_budget:
            overflow = prompt_tokens - self.total_budget
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
                        "before_tokens": current_budget,
                        "after_tokens": new_budget,
                        "overflow_tokens": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(
                    section_texts,
                    budgets,
                    selected_notes=selected_notes,
                    recent_runs=selected_recent_runs,
                    llm_history_compaction_enabled=llm_history_compaction_enabled,
                    dedup_file_paths=dedup_file_paths,
                )
                prompt = self._assemble_prompt(rendered)
                prompt_tokens = _estimate_tokens(prompt)
                reduced = True
                break
            if not reduced:
                break

        rendered, prompt = self._fit_current_request(rendered, user_message)

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            selected_recent_runs=selected_recent_runs,
            selected_skills=selected_skills,
            user_message=user_message,
            section_texts=section_texts,
            dynamic_adjustment=dynamic_adjustment,
        )
        return prompt, metadata

    def _render_sections_without_reduction(self, section_texts, selected_notes=None, recent_runs=None):
        selected_notes = selected_notes or []
        recent_runs = recent_runs or []
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(
                f"- {memorylib.render_relevant_memory_note(note)}"
                for note in selected_notes
                if memorylib.render_relevant_memory_note(note)
            )
        if recent_runs:
            relevant_lines.append("Recent runs:")
            relevant_lines.append(RECENT_RUN_GUIDANCE)
            relevant_lines.extend(f"- {self._render_recent_run(item)}" for item in recent_runs)
        if len(relevant_lines) == 1:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(self.agent.session.get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=_estimate_tokens(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=_estimate_tokens(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "skills": SectionRender(raw=section_texts["skills"], budget=_estimate_tokens(section_texts["skills"]), rendered=section_texts["skills"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=_estimate_tokens(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [memorylib.render_relevant_memory_note(note) for note in selected_notes],
                    "rendered_notes": [memorylib.render_relevant_memory_note(note) for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=_estimate_tokens(history_raw), rendered=history_raw, details={"rendered_entries": []}),
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

    def _render_sections(self, section_texts, budgets, selected_notes=None, recent_runs=None, llm_history_compaction_enabled=False, dedup_file_paths=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0), recent_runs=recent_runs or [])
            elif section == "history":
                rendered[section] = self._render_history_section(
                    int(budget or 0),
                    llm_history_compaction_enabled=llm_history_compaction_enabled,
                    dedup_file_paths=dedup_file_paths,
                )
            else:
                raw = section_texts[section]
                rendered_text = _token_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_relevant_memory(self, selected_notes, budget, recent_runs=None):
        header = "Relevant memory:"
        recent_runs = recent_runs or []
        note_texts = [
            memorylib.render_relevant_memory_note(note)
            for note in selected_notes
            if str(note.get("text", "")).strip()
        ]
        note_texts = [text for text in note_texts if str(text).strip()]
        run_texts = [self._render_recent_run(item) for item in recent_runs]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        if run_texts:
            raw_lines.append("Recent runs:")
            raw_lines.append(RECENT_RUN_GUIDANCE)
            raw_lines.extend(f"- {text}" for text in run_texts)
        raw = "\n".join(raw_lines) if (note_texts or run_texts) else "\n".join([header, "- none"])
        if not note_texts and not run_texts:
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
        if run_texts and not note_texts:
            per_run_budget = self._per_note_budget(budget, len(run_texts), header + "\nRecent runs:")
            rendered = "\n".join(
                [
                    header,
                    "Recent runs:",
                    RECENT_RUN_GUIDANCE,
                    *[f"- {_token_clip(text, per_run_budget)}" for text in run_texts],
                ]
            )
            if _estimate_tokens(rendered) > budget and budget > 0:
                rendered = _token_clip(raw, budget)
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": per_run_budget,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_token_clip(text, per_note_budget) for text in note_texts]
            lines = [header] + [f"- {text}" for text in rendered_notes]
            if run_texts:
                lines.append("Recent runs:")
                lines.append(RECENT_RUN_GUIDANCE)
                lines.extend(f"- {_token_clip(text, max(30, per_note_budget))}" for text in run_texts)
            rendered = "\n".join(lines)
            if _estimate_tokens(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if _estimate_tokens(rendered) > budget and budget > 0:
            rendered = _token_clip(raw, budget)
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
        overhead = _estimate_tokens(header) + note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget, llm_history_compaction_enabled=False, dedup_file_paths=None):
        return self.history_renderer._render_history_section(
            budget,
            llm_history_compaction_enabled=llm_history_compaction_enabled,
            dedup_file_paths=dedup_file_paths,
        )


    def _dynamic_budget_adjust(self, budgets, user_message):
        """根据用户请求特征动态调整 section budget 分配。

        核心思路：如果用户问的是"之前做了什么"，history 应该多分配；
        如果用户提到了具体文件名，memory 应该多分配。
        调整方式是从其他 section 等量借出，总预算不变。
        """
        msg_lower = str(user_message).lower()
        adjusted = dict(budgets)
        adjustment = {}

        history_signals = ("之前", "刚才", "上一次", "上一步", "已经", "before", "previous", "last time", "earlier", "already did")
        file_signals = (".py", ".js", ".ts", ".md", ".json", ".yaml", ".yml", ".txt", ".toml", "文件", "file")

        history_score = sum(1 for signal in history_signals if signal in msg_lower)
        file_score = sum(1 for signal in file_signals if signal in msg_lower)

        if history_score >= 2 and history_score > file_score:
            boost = min(800, int(budgets.get("prefix", 0) * 0.2))
            if boost > 0:
                adjusted["prefix"] = adjusted.get("prefix", 0) - boost
                adjusted["history"] = adjusted.get("history", 0) + boost
                adjustment = {"strategy": "history_boost", "boost_tokens": boost}
        elif file_score >= 2 and file_score > history_score:
            boost = min(400, int(budgets.get("skills", 0) * 0.3))
            if boost > 0:
                adjusted["skills"] = adjusted.get("skills", 0) - boost
                adjusted["memory"] = adjusted.get("memory", 0) + boost
                adjustment = {"strategy": "memory_boost", "boost_tokens": boost}

        return adjusted, adjustment

    def _raw_history_text(self, history):
        return self.history_renderer._raw_history_text(history)


    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        parts = []
        for section in SECTION_ORDER:
            text = rendered[section].rendered
            if section == "skills" and not str(text).strip():
                continue
            parts.append(text)
        return "\n\n".join(parts).strip()

    def _fit_current_request(self, rendered, user_message):
        """Clip only an oversized request, keeping both its beginning and end."""
        prompt = self._assemble_prompt(rendered)
        if _estimate_tokens(prompt) <= self.total_budget:
            return rendered, prompt

        header = "Current user request:\n"
        raw_section = header + user_message

        def section_for(char_limit):
            body = self._head_tail_clip(user_message, char_limit)
            return SectionRender(
                raw=raw_section,
                budget=0,
                rendered=header + body,
                details={"truncated": body != user_message},
            )

        rendered = dict(rendered)
        rendered[CURRENT_REQUEST_SECTION] = section_for(0)
        for section in self.reduction_order:
            overflow = _estimate_tokens(self._assemble_prompt(rendered)) - self.total_budget
            if overflow <= 0:
                break
            current = rendered[section]
            target_budget = max(0, current.rendered_tokens - overflow)
            rendered[section] = SectionRender(
                raw=current.raw,
                budget=target_budget,
                rendered=_token_clip(current.rendered, target_budget),
                details=current.details,
            )

        best = section_for(0)
        lo, hi = 0, len(user_message)
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = dict(rendered)
            candidate[CURRENT_REQUEST_SECTION] = section_for(mid)
            if _estimate_tokens(self._assemble_prompt(candidate)) <= self.total_budget:
                best = candidate[CURRENT_REQUEST_SECTION]
                lo = mid + 1
            else:
                hi = mid - 1

        rendered[CURRENT_REQUEST_SECTION] = best
        prompt = self._assemble_prompt(rendered)
        if _estimate_tokens(prompt) > self.total_budget:
            rendered[CURRENT_REQUEST_SECTION] = SectionRender(
                raw=raw_section,
                budget=self.total_budget,
                rendered=_token_clip(raw_section, self.total_budget),
                details={"truncated": True},
            )
            prompt = self._assemble_prompt(rendered)
        return rendered, prompt

    @staticmethod
    def _head_tail_clip(text, limit):
        text = str(text)
        limit = max(0, int(limit))
        if len(text) <= limit:
            return text
        marker = "\n... [request truncated] ...\n"
        if limit <= len(marker):
            return text[:limit]
        remaining = limit - len(marker)
        head = (remaining + 1) // 2
        tail = remaining // 2
        return text[:head] + marker + text[-tail:]

    def _metadata(
        self,
        prompt,
        rendered,
        budgets,
        reduction_log,
        selected_notes,
        selected_recent_runs,
        selected_skills,
        user_message,
        section_texts,
        dynamic_adjustment=None,
    ):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "rendered_chars": rendered[section].rendered_chars,
                "raw_estimated_tokens": rendered[section].raw_tokens,
                "budget_tokens": rendered[section].budget_tokens,
                "rendered_estimated_tokens": rendered[section].rendered_tokens,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            "raw_estimated_tokens": rendered[CURRENT_REQUEST_SECTION].raw_tokens,
            "budget_tokens": None,
            "rendered_estimated_tokens": rendered[CURRENT_REQUEST_SECTION].rendered_tokens,
        }
        prompt_tokens = _estimate_tokens(prompt)
        rendered_request = rendered[CURRENT_REQUEST_SECTION].rendered
        request_header = "Current user request:\n"
        rendered_request_body = (
            rendered_request[len(request_header):]
            if rendered_request.startswith(request_header)
            else rendered_request
        )
        return {
            "prompt_chars": len(prompt),
            "prompt_estimated_tokens": prompt_tokens,
            "prompt_budget_tokens": self.total_budget,
            "prompt_over_budget": prompt_tokens > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets_tokens": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "skills": skillslib.skill_metadata(selected_skills, rendered["skills"].rendered),
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
            "recent_runs": {
                "included": bool(selected_recent_runs),
                "selected_count": len(selected_recent_runs),
                "run_ids": [str(item.get("run_id", "")) for item in selected_recent_runs],
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
                "dedup_skipped": int(rendered["history"].details.get("dedup_skipped", 0)),
                "llm_compact_used": bool(rendered["history"].details.get("llm_compact_used", False)),
                "llm_compact_error": str(rendered["history"].details.get("llm_compact_error", "")),
                "llm_compact_summary_chars": int(rendered["history"].details.get("llm_compact_summary_chars", 0)),
            },
            "dynamic_adjustment": dict(dynamic_adjustment or {}),
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(rendered_request_body),
                "estimated_tokens": _estimate_tokens(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
                "truncated": rendered_request_body != user_message,
            },
        }

    def _recent_runs_for_request(self, user_message):
        if not self._is_resume_request(user_message):
            return []
        return list(self.agent.run_store.load_recent_index(limit=5))

    def _is_resume_request(self, user_message):
        text = str(user_message or "").lower()
        signals = (
            "继续",
            "刚才",
            "上次",
            "上一次",
            "之前",
            "前面",
            "那个 bug",
            "continue",
            "resume",
            "previous",
            "last time",
            "earlier",
        )
        return any(signal in text for signal in signals)

    def _render_recent_run(self, item):
        return (
            f"run_id={item.get('run_id', '')}; "
            f"task={item.get('task_goal', '')}; "
            f"status={item.get('status', '')}; "
            f"stop_reason={item.get('stop_reason', '')}; "
            f"updated_at={item.get('updated_at', '')}; "
            f"task_graph={item.get('task_graph_path', '')}; "
            f"report={item.get('report_path', '')}"
        )
