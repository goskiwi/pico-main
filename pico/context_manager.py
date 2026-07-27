"""Prompt 组装与上下文预算控制。

这个模块负责决定：新任务或任务内 checkpoint compaction 后，把多少
prefix、memory、任务控制状态和当前用户请求送进模型。正常工具回合由
provider 的连续 tool-result 会话承载，不会用画布摘要替代原始证据。
"""

from __future__ import annotations

import re

from . import skills as skillslib
from .config import (
    DEFAULT_TOTAL_BUDGET,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_REDUCTION_ORDER,
)
from .context_types import (
    SectionRender,
    _token_clip,
)


SECTION_ORDER = (
    "prefix",
    "memory",
    "skills",
    "repo_map",
    "task_context",
    "current_request",
)
CURRENT_REQUEST_SECTION = "current_request"
RECENT_RUN_GUIDANCE = (
    "Use read_task_canvas for the task map or an archived phase, read_task_event for a node summary, "
    "then read_tool_output only when the evidence is needed."
)


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
        self.repo_map_budget_cap_tokens = agent.repo_map_budget_tokens
        if self.repo_map_budget_cap_tokens is not None:
            self.section_budgets["repo_map"] = int(
                self.repo_map_budget_cap_tokens
            )
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)
        self._count_tokens = agent.count_tokens

    def _section(self, *, raw, budget, rendered, details=None):
        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details=details,
            token_counter=self._count_tokens,
        )

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        任务控制状态 + 当前请求”拼成首次（或超限恢复时）发给模型的
        prompt。任务内的后续回合继续使用 provider 保存的精确 tool-result 对话；
        画布只是可审计、可下钻的控制平面。

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
        repo_map_enabled = self.agent.feature_enabled("repo_map")
        context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        dynamic_budget_enabled = self.agent.feature_enabled("dynamic_budget")
        selected_recent_runs = self._recent_runs_for_request(user_message)
        repo_map_query = self.agent.repo_map.query(user_message) if repo_map_enabled else None
        prefix_text = str(self.agent.prefix)
        checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            prefix_text = checkpoint_text + "\n\n" + prefix_text
        section_texts = {
            "prefix": prefix_text,
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "skills": "",
            "repo_map": "Repository map:\n- disabled",
            "task_context": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        section_texts["skills"] = "\n\n".join(
            item
            for item in (
                skillslib.render_skill_index(self.agent.skills),
                self.agent.active_skill_instructions(),
            )
            if item
        )

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(
                section_texts,
                recent_runs=selected_recent_runs,
                repo_map_query=repo_map_query,
            )
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_recent_runs=selected_recent_runs,
                user_message=user_message,
                section_texts=section_texts,
                dynamic_adjustment={},
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        dynamic_adjustment = {}
        if dynamic_budget_enabled:
            budgets, dynamic_adjustment = self._dynamic_budget_adjust(budgets, user_message)
        if self.repo_map_budget_cap_tokens is not None:
            repo_map_budget_before_cap = int(budgets.get("repo_map", 0))
            budgets["repo_map"] = min(
                repo_map_budget_before_cap,
                int(self.repo_map_budget_cap_tokens),
            )
            dynamic_adjustment = {
                **dynamic_adjustment,
                "repo_map_budget_cap_tokens": int(
                    self.repo_map_budget_cap_tokens
                ),
                "repo_map_budget_before_cap_tokens": repo_map_budget_before_cap,
            }
        rendered = self._render_sections(
            section_texts,
            budgets,
            recent_runs=selected_recent_runs,
            repo_map_query=repo_map_query,
        )
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超过 token 预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 skills 和任务画布，然后才动 repo map、
        # memory 和 prefix。
        # 优先压缩旧上下文；仍然超预算时，保留当前请求的首尾并截断。
        prompt_tokens = self._count_tokens(prompt)
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
                    recent_runs=selected_recent_runs,
                    repo_map_query=repo_map_query,
                )
                prompt = self._assemble_prompt(rendered)
                prompt_tokens = self._count_tokens(prompt)
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
            selected_recent_runs=selected_recent_runs,
            user_message=user_message,
            section_texts=section_texts,
            dynamic_adjustment=dynamic_adjustment,
        )
        return prompt, metadata

    def _render_sections_without_reduction(
        self,
        section_texts,
        recent_runs=None,
        repo_map_query=None,
    ):
        recent_runs = recent_runs or []
        task_context_raw = self._task_context_raw(recent_runs)
        return {
            "prefix": self._section(raw=section_texts["prefix"], budget=self._count_tokens(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": self._section(raw=section_texts["memory"], budget=self._count_tokens(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "skills": self._section(raw=section_texts["skills"], budget=self._count_tokens(section_texts["skills"]), rendered=section_texts["skills"], details={}),
            "repo_map": self._render_repo_map(
                repo_map_query,
                int(self.section_budgets.get("repo_map", 0)),
                section_texts["repo_map"],
            ),
            "task_context": self._section(
                raw=task_context_raw,
                budget=self._count_tokens(task_context_raw),
                rendered=task_context_raw,
                details={
                    "active_run_id": self._active_run_id(),
                    "recent_run_count": len(recent_runs),
                },
            ),
            CURRENT_REQUEST_SECTION: self._section(
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

    def _render_sections(
        self,
        section_texts,
        budgets,
        recent_runs=None,
        repo_map_query=None,
    ):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = self._section(raw=raw, budget=0, rendered=raw, details={})
            elif section == "repo_map":
                rendered[section] = self._render_repo_map(
                    repo_map_query,
                    int(budget or 0),
                    section_texts[section],
                )
            elif section == "task_context":
                rendered[section] = self._render_task_context(int(budget or 0), recent_runs=recent_runs or [])
            else:
                raw = section_texts[section]
                rendered_text = _token_clip(raw, int(budget), token_counter=self._count_tokens) if budget is not None else raw
                rendered[section] = self._section(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_repo_map(self, repo_map_query, budget, fallback):
        budget = max(0, int(budget))
        if repo_map_query is None:
            rendered = _token_clip(fallback, budget, token_counter=self._count_tokens) if budget else ""
            return self._section(
                raw=fallback,
                budget=budget,
                rendered=rendered,
                details={
                    "enabled": False,
                    "selected_count": 0,
                    "selected_files": [],
                    "selected_symbols": [],
                },
            )
        raw_render = repo_map_query.render(
            budget_tokens=max(4000, budget),
            max_results=60,
            token_counter=self._count_tokens,
        )
        selected_render = repo_map_query.render(
            budget_tokens=budget,
            max_results=24,
            token_counter=self._count_tokens,
        )
        return self._section(
            raw=raw_render.text,
            budget=budget,
            rendered=selected_render.text,
            details={"enabled": True, **selected_render.details},
        )

    def _task_context_raw(self, recent_runs):
        raw = self.agent.task_context_text()
        if not recent_runs:
            return raw
        recent_run_lines = [
            "Recent runs:",
            RECENT_RUN_GUIDANCE,
            *[f"- {self._render_recent_run(item)}" for item in recent_runs],
        ]
        return "\n\n".join([raw, "\n".join(recent_run_lines)]) if raw else "\n".join(recent_run_lines)

    def _render_task_context(self, budget, recent_runs=None):
        recent_runs = recent_runs or []
        raw = self._task_context_raw(recent_runs)
        return self._section(
            raw=raw,
            budget=budget,
            rendered=_token_clip(raw, budget, token_counter=self._count_tokens) if budget else "",
            details={
                "active_run_id": self._active_run_id(),
                "recent_run_count": len(recent_runs),
            },
        )

    def _active_run_id(self):
        state = self.agent.current_task_state
        return str(state.run_id) if state is not None else ""


    def _dynamic_budget_adjust(self, budgets, user_message):
        """根据用户请求特征动态调整 section budget 分配。

        核心思路：如果用户问的是"之前做了什么"，任务控制状态应该多分配；
        如果用户提到了具体文件名，memory 应该多分配。
        调整方式是从其他 section 等量借出，总预算不变。
        """
        msg_lower = str(user_message).lower()
        adjusted = dict(budgets)
        adjustment = {}

        task_context_signals = ("之前", "刚才", "上一次", "上一步", "已经", "before", "previous", "last time", "earlier", "already did")
        file_signals = (".py", ".js", ".ts", ".md", ".json", ".yaml", ".yml", ".txt", ".toml", "文件", "file")

        task_context_score = sum(1 for signal in task_context_signals if signal in msg_lower)
        file_score = sum(1 for signal in file_signals if signal in msg_lower)
        if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b", str(user_message)):
            file_score += 2

        if task_context_score >= 2 and task_context_score > file_score:
            boost = min(800, int(budgets.get("prefix", 0) * 0.2))
            if boost > 0:
                adjusted["prefix"] = adjusted.get("prefix", 0) - boost
                adjusted["task_context"] = adjusted.get("task_context", 0) + boost
                adjustment = {"strategy": "task_context_boost", "boost_tokens": boost}
        elif file_score >= 1 and file_score > task_context_score:
            boost = min(600, int(budgets.get("task_context", 0) * 0.15))
            if boost > 0:
                adjusted["task_context"] = adjusted.get("task_context", 0) - boost
                adjusted["repo_map"] = adjusted.get("repo_map", 0) + boost
                adjustment = {"strategy": "repo_map_boost", "boost_tokens": boost}

        return adjusted, adjustment

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
        if self._count_tokens(prompt) <= self.total_budget:
            return rendered, prompt

        header = "Current user request:\n"
        raw_section = header + user_message

        def section_for(char_limit):
            body = self._head_tail_clip(user_message, char_limit)
            return self._section(
                raw=raw_section,
                budget=0,
                rendered=header + body,
                details={"truncated": body != user_message},
            )

        rendered = dict(rendered)
        rendered[CURRENT_REQUEST_SECTION] = section_for(0)
        for section in self.reduction_order:
            overflow = self._count_tokens(self._assemble_prompt(rendered)) - self.total_budget
            if overflow <= 0:
                break
            current = rendered[section]
            target_budget = max(0, current.rendered_tokens - overflow)
            rendered[section] = self._section(
                raw=current.raw,
                budget=target_budget,
                rendered=_token_clip(current.rendered, target_budget, token_counter=self._count_tokens),
                details=current.details,
            )

        best = section_for(0)
        lo, hi = 0, len(user_message)
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = dict(rendered)
            candidate[CURRENT_REQUEST_SECTION] = section_for(mid)
            if self._count_tokens(self._assemble_prompt(candidate)) <= self.total_budget:
                best = candidate[CURRENT_REQUEST_SECTION]
                lo = mid + 1
            else:
                hi = mid - 1

        rendered[CURRENT_REQUEST_SECTION] = best
        prompt = self._assemble_prompt(rendered)
        if self._count_tokens(prompt) > self.total_budget:
            rendered[CURRENT_REQUEST_SECTION] = self._section(
                raw=raw_section,
                budget=self.total_budget,
                rendered=_token_clip(raw_section, self.total_budget, token_counter=self._count_tokens),
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
        selected_recent_runs,
        user_message,
        section_texts,
        dynamic_adjustment=None,
    ):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "rendered_chars": rendered[section].rendered_chars,
                "raw_tokens": rendered[section].raw_tokens,
                "budget_tokens": rendered[section].budget_tokens,
                "rendered_tokens": rendered[section].rendered_tokens,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            "raw_tokens": rendered[CURRENT_REQUEST_SECTION].raw_tokens,
            "budget_tokens": None,
            "rendered_tokens": rendered[CURRENT_REQUEST_SECTION].rendered_tokens,
        }
        prompt_tokens = self._count_tokens(prompt)
        rendered_request = rendered[CURRENT_REQUEST_SECTION].rendered
        request_header = "Current user request:\n"
        rendered_request_body = (
            rendered_request[len(request_header):]
            if rendered_request.startswith(request_header)
            else rendered_request
        )
        return {
            "prompt_chars": len(prompt),
            "prompt_tokens": prompt_tokens,
            "prompt_budget_tokens": self.total_budget,
            "prompt_over_budget": prompt_tokens > self.total_budget,
            "tokenizer": self.agent.tokenizer_metadata(),
            "section_order": list(SECTION_ORDER),
            "section_budgets_tokens": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "skills": self.agent.skill_metadata(rendered["skills"].rendered),
            "repo_map": {
                "enabled": bool(rendered["repo_map"].details.get("enabled", False)),
                "query": str(rendered["repo_map"].details.get("query", "")),
                "graph_nodes": int(rendered["repo_map"].details.get("graph_nodes", 0)),
                "graph_edges": int(rendered["repo_map"].details.get("graph_edges", 0)),
                "parsed_files": int(rendered["repo_map"].details.get("parsed_files", 0)),
                "skipped_files": int(rendered["repo_map"].details.get("skipped_files", 0)),
                "parse_error_files": int(rendered["repo_map"].details.get("parse_error_files", 0)),
                "cache_hits": int(rendered["repo_map"].details.get("cache_hits", 0)),
                "cache_misses": int(rendered["repo_map"].details.get("cache_misses", 0)),
                "selected_count": int(rendered["repo_map"].details.get("selected_count", 0)),
                "selected_files": list(rendered["repo_map"].details.get("selected_files", [])),
                "selected_symbols": list(rendered["repo_map"].details.get("selected_symbols", [])),
                "truncated": bool(rendered["repo_map"].details.get("truncated", False)),
                "raw_tokens": rendered["repo_map"].raw_tokens,
                "rendered_tokens": rendered["repo_map"].rendered_tokens,
            },
            "recent_runs": {
                "included": bool(selected_recent_runs),
                "selected_count": len(selected_recent_runs),
                "run_ids": [str(item.get("run_id", "")) for item in selected_recent_runs],
            },
            "task_context": {
                "raw_chars": rendered["task_context"].raw_chars,
                "rendered_chars": rendered["task_context"].rendered_chars,
                "raw_tokens": rendered["task_context"].raw_tokens,
                "rendered_tokens": rendered["task_context"].rendered_tokens,
                "active_run_id": str(rendered["task_context"].details.get("active_run_id", "")),
                "recent_run_count": int(rendered["task_context"].details.get("recent_run_count", 0)),
            },
            "dynamic_adjustment": dict(dynamic_adjustment or {}),
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(rendered_request_body),
                "tokens": self._count_tokens(user_message),
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
            f"latest_node={item.get('latest_node_id', '')}; "
            f"task_canvas={item.get('task_canvas_path', '')}; "
            f"offload={item.get('offload_path', '')}; "
            f"report={item.get('report_path', '')}"
        )
