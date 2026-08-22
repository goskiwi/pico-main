"""Tokenizer-aware context assembly with shared budgets and ranked projections."""

from __future__ import annotations

from dataclasses import dataclass

import tiktoken

from .features.memory import WorkingState

DEFAULT_SECTION_BUDGETS = {
    "prefix": 2000,
    "memory_catalog": 600,
    "repo_map": 1200,
    "working_state": 300,
    "history": 2600,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 600,
    "memory_catalog": 80,
    "repo_map": 120,
    "working_state": 60,
    "history": 500,
}
DEFAULT_REDUCTION_ORDER = (
    "repo_map",
    "history",
    "memory_catalog",
    "working_state",
    "prefix",
)
SECTION_ORDER = (
    "prefix",
    "memory_catalog",
    "repo_map",
    "working_state",
    "history",
    "current_request",
)
SECTION_WEIGHTS = {
    "prefix": 4,
    "memory_catalog": 2,
    "repo_map": 3,
    "working_state": 2,
    "history": 4,
}


class ContextBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedSection:
    raw: str
    budget_tokens: int | None
    rendered: str


class Tokenizer:
    def __init__(self, model=""):
        try:
            self.encoding = tiktoken.encoding_for_model(str(model or ""))
        except KeyError:
            self.encoding = tiktoken.get_encoding("o200k_base")

    def count(self, text):
        return len(self.encoding.encode(str(text or ""), disallowed_special=()))

    def clip(self, text, limit):
        text = str(text or "")
        tokens = self.encoding.encode(text, disallowed_special=())
        if len(tokens) <= limit:
            return text
        if limit <= 3:
            return self.encoding.decode(tokens[: max(0, limit)])
        return self.encoding.decode(tokens[: limit - 2]).rstrip() + " …"


class ContextManager:
    """Compile stable rules, memory, current Run Log and request."""

    def __init__(
        self,
        agent,
        total_budget=None,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
        compaction_reserve_tokens=None,
        compaction_keep_recent_tokens=None,
    ):
        self.agent = agent
        self.total_budget = int(
            agent.config.provider_context_limit_tokens
            if total_budget is None
            else total_budget
        )
        self.compaction_reserve_tokens = int(
            agent.config.compaction_reserve_tokens
            if compaction_reserve_tokens is None
            else compaction_reserve_tokens
        )
        self.compaction_keep_recent_tokens = int(
            agent.config.compaction_keep_recent_tokens
            if compaction_keep_recent_tokens is None
            else compaction_keep_recent_tokens
        )
        self.section_budgets = {**DEFAULT_SECTION_BUDGETS, **dict(section_budgets or {})}
        requested_floors = {**DEFAULT_SECTION_FLOORS, **dict(section_floors or {})}
        self.section_floors = {
            key: min(int(requested_floors[key]), max(8, int(self.section_budgets[key]) // 3))
            for key in self.section_budgets
        }
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)
        model = str(getattr(getattr(agent, "model_client", None), "model", ""))
        self.tokenizer = Tokenizer(model)
        self._last_history_metadata = {}

    def build(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        provider_overhead_tokens=0,
    ):
        request = f"Current user request:\n{user_message!s}"
        output_reserve = int(self.agent.config.max_new_tokens)
        tool_schema_tokens = self._tool_schema_tokens()
        provider_overhead_tokens = max(0, int(provider_overhead_tokens or 0))
        request_overhead_tokens = tool_schema_tokens + provider_overhead_tokens
        if (
            self.tokenizer.count(request)
            + output_reserve
            + request_overhead_tokens
            >= self.total_budget
        ):
            raise ContextBudgetExceeded("current request and output reservation exceed the model budget")

        raw = {
            "prefix": self._prefix_text(),
            "memory_catalog": self._memory_catalog_text(),
            "repo_map": self._repo_map_text(user_message),
            "working_state": self._working_state_text(user_message),
            "history": self._history_text(user_message),
            "current_request": request,
        }
        compaction_metadata = self._compact_run_log_if_needed(
            raw,
            provider_context_tokens=provider_context_tokens,
            request_overhead_tokens=request_overhead_tokens,
        )
        if compaction_metadata is not None:
            raw["history"] = self._history_text(user_message)
        available = self.total_budget - output_reserve - request_overhead_tokens
        budgets, allocation = self._allocate_budgets(raw, available)
        rendered = self._render(raw, budgets)
        prompt = self._assemble(rendered)
        reductions = list(allocation["reductions"])
        while self.tokenizer.count(prompt) > available:
            overflow = self.tokenizer.count(prompt) - available
            changed = False
            for section in self.reduction_order:
                before = budgets[section]
                floor = min(
                    self._effective_floor(section),
                    self.tokenizer.count(raw[section]),
                )
                after = max(floor, before - overflow)
                if after >= before:
                    continue
                budgets[section] = after
                reductions.append(
                    {
                        "section": section,
                        "before_tokens": before,
                        "after_tokens": after,
                        "overflow_tokens": overflow,
                    }
                )
                changed = True
                break
            if not changed:
                raise ContextBudgetExceeded(
                    "required context sections cannot fit inside token floors"
                )
            rendered = self._render(raw, budgets)
            prompt = self._assemble(rendered)
        allocation["allocated_tokens"] = dict(budgets)
        allocation["borrowed_tokens"] = {
            key: max(0, budgets[key] - int(self.section_budgets[key]))
            for key in budgets
        }

        sections = {
            key: {
                "raw_tokens": self.tokenizer.count(value.raw),
                "budget_tokens": value.budget_tokens,
                "rendered_tokens": self.tokenizer.count(value.rendered),
            }
            for key, value in rendered.items()
        }
        prompt_tokens = self.tokenizer.count(prompt)
        estimated_input_tokens = prompt_tokens + request_overhead_tokens
        metadata = {
            "prompt_tokens": prompt_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "provider_overhead_tokens": provider_overhead_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "reserved_output_tokens": output_reserve,
            "within_budget": estimated_input_tokens + output_reserve <= self.total_budget,
            "tokenizer": self.tokenizer.encoding.name,
            "section_order": list(SECTION_ORDER),
            "sections": sections,
            "budget_allocation": allocation,
            "budget_reductions": reductions,
            "history_projection": dict(self._last_history_metadata),
            "run_log_generation": int(
                getattr(self.agent.run.run_log, "generation", 0)
            ),
            "compaction": compaction_metadata,
            "provider_context_tokens": provider_context_tokens,
        }
        return prompt, metadata

    def _tool_schema_tokens(self):
        estimator = getattr(
            self.agent.model_client,
            "estimate_action_tool_tokens",
            None,
        )
        if estimator is None:
            return 0
        return max(
            0,
            int(
                estimator(
                    self.agent.tools.action_schemas,
                    self.tokenizer.count,
                )
            ),
        )

    def _allocate_budgets(self, raw, available):
        sections = tuple(self.section_budgets)
        raw_tokens = {section: self.tokenizer.count(raw[section]) for section in sections}
        request_tokens = self.tokenizer.count(raw["current_request"])
        separator_tokens = self.tokenizer.count("\n\n" * (len(SECTION_ORDER) - 1))
        pool = available - request_tokens - separator_tokens
        floors = {section: min(raw_tokens[section], self._effective_floor(section)) for section in sections}
        if sum(floors.values()) > pool:
            raise ContextBudgetExceeded("required context section floors exceed the input budget")
        allocated = dict(floors)
        remaining = pool - sum(allocated.values())
        preferred = {
            section: min(raw_tokens[section], int(self.section_budgets[section])) for section in sections
        }
        remaining = self._distribute(allocated, preferred, remaining)
        remaining = self._distribute(allocated, raw_tokens, remaining)
        reductions = []
        for section in self.reduction_order:
            if raw_tokens[section] <= allocated[section]:
                continue
            reductions.append(
                {
                    "section": section,
                    "before_tokens": raw_tokens[section],
                    "after_tokens": allocated[section],
                    "overflow_tokens": raw_tokens[section] - allocated[section],
                }
            )
        return allocated, {
            "strategy": "floor_weighted_shared_pool",
            "pool_tokens": pool,
            "base_floor_tokens": floors,
            "preferred_tokens": dict(self.section_budgets),
            "raw_tokens": raw_tokens,
            "allocated_tokens": dict(allocated),
            "borrowed_tokens": {
                section: max(0, allocated[section] - int(self.section_budgets[section])) for section in sections
            },
            "unused_shared_tokens": remaining,
            "reductions": reductions,
        }

    def _effective_floor(self, section):
        preferred = int(self.section_budgets[section])
        return min(int(self.section_floors[section]), max(8, preferred // 3))

    @staticmethod
    def _distribute(allocated, caps, remaining):
        while remaining > 0:
            active = [section for section in allocated if allocated[section] < caps[section]]
            if not active:
                break
            total_weight = sum(SECTION_WEIGHTS[section] for section in active)
            progressed = False
            for section in active:
                share = max(1, remaining * SECTION_WEIGHTS[section] // total_weight)
                grant = min(share, caps[section] - allocated[section], remaining)
                if grant:
                    allocated[section] += grant
                    remaining -= grant
                    progressed = True
            if not progressed:
                break
        return remaining

    def _render(self, raw, budgets):
        return {
            section: RenderedSection(
                raw=text,
                budget_tokens=None if section == "current_request" else budgets[section],
                rendered=text if section == "current_request" else self.tokenizer.clip(text, budgets[section]),
            )
            for section, text in raw.items()
        }

    @staticmethod
    def _assemble(rendered):
        return "\n\n".join(rendered[section].rendered for section in SECTION_ORDER).strip()

    def _prefix_text(self):
        return str(self.agent.prompt.prefix)

    def _working_state_text(self, current_request):
        task_state = self.agent.run.task_state
        if task_state is None:
            return WorkingState().render_panel()
        working_state = task_state.working_state
        return str(
            working_state.render_panel(
                include_goal=working_state.goal != str(current_request)
            )
        )

    def _memory_catalog_text(self):
        return str(self.agent.dependencies.project_memory.index_text())

    def _repo_map_text(self, query):
        repo_map = self.agent.dependencies.repo_map
        if repo_map is None:
            return "Repository map:\n- unavailable"
        result = repo_map.render(
            query,
            budget_tokens=int(self.section_budgets.get("repo_map", 1200)),
            max_results=24,
            token_counter=self.tokenizer.count,
        )
        return result.text

    def _history_text(self, current_request):
        run_log = self.agent.run.run_log
        if run_log is None:
            self._last_history_metadata = {
                "active_count": 0,
                "selected_count": 0,
                "omitted_count": 0,
                "artifact_references": 0,
            }
            return "Current run events:\n- empty"
        history, metadata = run_log.render_projection(
            current_request,
            exclude_user_content=current_request,
        )
        self._last_history_metadata = metadata
        return history

    def _compact_run_log_if_needed(
        self,
        raw,
        *,
        provider_context_tokens=None,
        request_overhead_tokens=0,
    ):
        run_log = self.agent.run.run_log
        if run_log is None or run_log.pending_call_id():
            return
        separator_tokens = self.tokenizer.count("\n\n" * (len(SECTION_ORDER) - 1))
        local_context_tokens = (
            sum(self.tokenizer.count(text) for text in raw.values())
            + separator_tokens
            + max(0, int(request_overhead_tokens or 0))
        )
        context_tokens = max(
            local_context_tokens,
            int(provider_context_tokens or 0),
        )
        reserve_tokens = max(
            int(self.agent.config.max_new_tokens),
            self.compaction_reserve_tokens,
        )
        threshold_tokens = max(1, self.total_budget - reserve_tokens)
        if context_tokens <= threshold_tokens:
            return
        compacted = run_log.compact(
            retain_tokens=self.compaction_keep_recent_tokens,
            token_counter=self.tokenizer.count,
        )
        if compacted is None:
            return
        event, metadata = compacted
        self.agent.apply_run_event(event)
        return {
            **metadata,
            "trigger_context_tokens": context_tokens,
            "local_context_tokens": local_context_tokens,
            "trigger_threshold_tokens": threshold_tokens,
        }
