"""Tokenizer-aware context assembly with shared budgets and ranked projections."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import tiktoken

DEFAULT_TOTAL_BUDGET = 8000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 2000,
    "repo_map": 1200,
    "memory": 700,
    "relevant_memory": 700,
    "history": 2600,
}
DEFAULT_SECTION_FLOORS = {"prefix": 600, "repo_map": 120, "memory": 120, "relevant_memory": 80, "history": 500}
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "repo_map", "history", "memory", "prefix")
SECTION_ORDER = ("prefix", "repo_map", "memory", "relevant_memory", "history", "current_request")
SECTION_WEIGHTS = {"prefix": 4, "repo_map": 3, "memory": 2, "relevant_memory": 2, "history": 4}
RELEVANT_MEMORY_LIMIT = 4


class ContextBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class SectionRender:
    raw: str
    budget_tokens: int | None
    rendered: str


class Tokenizer:
    def __init__(self, model=""):
        try:
            self.encoding = tiktoken.encoding_for_model(str(model or ""))
            self.model_mapping_known = True
        except KeyError:
            self.encoding = tiktoken.get_encoding("o200k_base")
            self.model_mapping_known = False

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
    """Compile stable rules, memory, prior runs, current ledger and request."""

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

    def build(self, user_message):
        request = f"Current user request:\n{user_message!s}"
        output_reserve = int(getattr(self.agent, "max_new_tokens", 0))
        if self.tokenizer.count(request) + output_reserve >= self.total_budget:
            raise ContextBudgetExceeded("current request and output reservation exceed the model budget")

        self._compact_ledger_if_needed()
        relevant_text, memory_retrieval = self._relevant_notes(user_message)
        raw = {
            "prefix": self._prefix_text(),
            "repo_map": self._repo_map_text(user_message),
            "memory": self._memory_text(),
            "relevant_memory": relevant_text,
            "history": self._history_text(user_message),
            "current_request": request,
        }
        available = self.total_budget - output_reserve
        reduction_enabled = not hasattr(self.agent, "feature_enabled") or self.agent.feature_enabled("context_reduction")

        if reduction_enabled:
            budgets, allocation = self._allocate_budgets(raw, available)
            rendered = self._render(raw, budgets)
            prompt = self._assemble(rendered)
            reductions = list(allocation["reductions"])
            while self.tokenizer.count(prompt) > available:
                overflow = self.tokenizer.count(prompt) - available
                changed = False
                for section in self.reduction_order:
                    before = budgets[section]
                    floor = min(self._effective_floor(section), self.tokenizer.count(raw[section]))
                    after = max(floor, before - overflow)
                    if after >= before:
                        continue
                    budgets[section] = after
                    reductions.append(
                        {"section": section, "before_tokens": before, "after_tokens": after, "overflow_tokens": overflow}
                    )
                    changed = True
                    break
                if not changed:
                    raise ContextBudgetExceeded("required context sections cannot fit inside token floors")
                rendered = self._render(raw, budgets)
                prompt = self._assemble(rendered)
            allocation["allocated_tokens"] = dict(budgets)
            allocation["borrowed_tokens"] = {
                key: max(0, budgets[key] - int(self.section_budgets[key])) for key in budgets
            }
        else:
            budgets = {section: self.tokenizer.count(text) for section, text in raw.items() if section != "current_request"}
            rendered = {
                section: SectionRender(text, None if section == "current_request" else budgets[section], text)
                for section, text in raw.items()
            }
            prompt = self._assemble(rendered)
            reductions = []
            allocation = {
                "strategy": "unbounded_ablation",
                "pool_tokens": sum(budgets.values()),
                "allocated_tokens": budgets,
                "borrowed_tokens": {key: 0 for key in budgets},
                "unused_shared_tokens": 0,
                "reductions": [],
            }

        sections = {
            key: {
                "raw_tokens": self.tokenizer.count(value.raw),
                "budget_tokens": value.budget_tokens,
                "rendered_tokens": self.tokenizer.count(value.rendered),
            }
            for key, value in rendered.items()
        }
        metadata = {
            "governance_version": "context-governance-v3",
            "prompt_tokens": self.tokenizer.count(prompt),
            "input_limit_tokens": self.total_budget,
            "reserved_output_tokens": output_reserve,
            "within_budget": self.tokenizer.count(prompt) + output_reserve <= self.total_budget,
            "tokenizer": self.tokenizer.encoding.name,
            "model_mapping_known": self.tokenizer.model_mapping_known,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {**budgets, "current_request": None},
            "sections": sections,
            "budget_allocation": allocation,
            "budget_reductions": reductions,
            "reduction_order": list(self.reduction_order),
            "history_projection": dict(self._last_history_metadata),
            "memory_retrieval": dict(memory_retrieval),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(memory_retrieval.get("selected_filenames", [])),
                "selected_filenames": list(memory_retrieval.get("selected_filenames", [])),
            },
            "current_request": {"text": str(user_message), "tokens": self.tokenizer.count(str(user_message))},
            "ledger_generation": int(getattr(getattr(self.agent, "context_ledger", None), "generation", 0)),
        }
        return prompt, metadata

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
            section: SectionRender(
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
        text = str(getattr(self.agent, "prefix", ""))
        checkpoint = str(self.agent.render_checkpoint_text() or "").strip()
        return text + ("\n\n" + checkpoint if checkpoint else "")

    def _memory_text(self):
        if hasattr(self.agent, "feature_enabled") and not self.agent.feature_enabled("memory"):
            return "Memory:\n- disabled"
        return str(self.agent.memory.render_panel())

    def _repo_map_text(self, query):
        repo_map = getattr(self.agent, "repo_map", None)
        if repo_map is None:
            return "Repository map:\n- unavailable"
        result = repo_map.render(
            query,
            budget_tokens=int(self.section_budgets.get("repo_map", 1200)),
            max_results=24,
            token_counter=self.tokenizer.count,
        )
        return result.text

    def _relevant_notes(self, query):
        if hasattr(self.agent, "feature_enabled") and not self.agent.feature_enabled("relevant_memory"):
            return "Project memory:\n- disabled", {"selected_filenames": []}
        return self.agent.select_memory_for_task(query)

    @staticmethod
    def _query_tokens(query):
        return set(re.findall(r"[A-Za-z0-9_./-]+|[\u4e00-\u9fff]", str(query).lower()))

    def _rank_prior_history(self, history, query, recent_limit=4, relevant_limit=4):
        if len(history) <= recent_limit:
            return history
        query_tokens = self._query_tokens(query)
        selected = set(range(len(history) - recent_limit, len(history)))
        scored = []
        for index, item in enumerate(history[:-recent_limit]):
            text = " ".join(
                [str(item.get("content", "")), str(item.get("name", "")), json.dumps(item.get("args", {}), sort_keys=True)]
            )
            overlap = len(query_tokens & self._query_tokens(text))
            if overlap:
                scored.append((overlap, index))
        selected.update(index for _, index in sorted(scored, reverse=True)[:relevant_limit])
        return [item for index, item in enumerate(history) if index in selected]

    def _history_text(self, current_request):
        ledger = getattr(self.agent, "context_ledger", None)
        current_run_id = str(getattr(ledger, "run_id", ""))
        all_history = list(getattr(self.agent, "session", {}).get("history", []))
        prior = [item for item in all_history if not current_run_id or item.get("run_id") != current_run_id]
        selected_prior = self._rank_prior_history(prior, current_request)
        prior_lines = ["Prior session context:"]
        for item in selected_prior:
            content = str(item.get("content", ""))
            if item.get("role") == "run_summary":
                changed = ", ".join(item.get("changed_paths", [])) or "none"
                content += (
                    f" | changed: {changed}"
                    f" | verification: {item.get('verification_status', 'unknown')}"
                    f" | stop: {item.get('stop_reason', '')}"
                )
            prior_lines.append(
                f"[prior/{item.get('role', '')}] {self.tokenizer.clip(content, 220)}"
            )
        if len(prior_lines) == 1:
            prior_lines.append("- empty")
        ledger_metadata = {"active_count": 0, "selected_count": 0, "omitted_count": 0, "artifact_references": 0}
        if ledger is not None:
            ledger_text, ledger_metadata = ledger.render_projection(
                current_request,
                exclude_user_content=current_request,
            )
            lines = [ledger_text, "", *prior_lines]
        else:
            lines = prior_lines
        self._last_history_metadata = {
            "source": "ledger_plus_prior_runs" if ledger is not None else "prior_session_only",
            "prior_total_count": len(prior),
            "prior_selected_count": len(selected_prior),
            "ledger": ledger_metadata,
        }
        return "\n".join(lines)

    def _compact_ledger_if_needed(self):
        ledger = getattr(self.agent, "context_ledger", None)
        if ledger is None or ledger.pending_call_id():
            return
        active = ledger.active_entries()
        projection, _ = ledger.render_projection("")
        if len(active) <= 10 and self.tokenizer.count(projection) <= self.section_budgets["history"]:
            return
        regions = ledger.compaction_regions(retain_units=3)
        compact_history = regions["compact_history"]
        if not compact_history or regions["raw_tail"]:
            return
        expected_generation = ledger.generation
        expected_digest = ledger.active_digest()
        workspace_before = self.agent.capture_workspace_snapshot()
        workspace_digest = hashlib.sha256(
            json.dumps(workspace_before, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        summary = ledger.build_structured_summary(compact_history)
        rendered_summary = ledger._render_summary(summary)
        source_text = "\n".join(ledger._entry_search_text(entry) for entry in compact_history)
        if self.tokenizer.count(rendered_summary) >= self.tokenizer.count(source_text):
            return
        workspace_after = self.agent.capture_workspace_snapshot()
        after_digest = hashlib.sha256(
            json.dumps(workspace_after, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if workspace_digest != after_digest:
            return
        ledger.commit_compaction(
            summary,
            [entry.entry_id for entry in compact_history],
            expected_generation=expected_generation,
            expected_active_digest=expected_digest,
        )
