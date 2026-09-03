"""Tokenizer-aware, read-only prompt input assembly."""

from __future__ import annotations

import json
from html import escape

import tiktoken

from .compaction_summary import CompactionSummarizer, SemanticCompactionError
from .run_log import COMPACTED_HISTORY_OMITTED
from .working_state import WorkingState

DEFAULT_SECTION_CAPS = {
    "workspace": 600,
    "repo_map": 1200,
    "working_state": 300,
}
FIXED_SECTION_ALLOCATION_ORDER = (
    "workspace",
    "repo_map",
    "working_state",
)
CONTEXT_ALLOCATION_ORDER = (
    *FIXED_SECTION_ALLOCATION_ORDER,
    "history",
)
CONTEXT_WIRE_ORDER = (
    "workspace",
    "repo_map",
    "history",
    "working_state",
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

class _ContextAssembler:
    """Internal dynamic-input assembly used only by PromptBuilder."""

    def __init__(
        self,
        agent,
        total_budget=None,
        section_caps=None,
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
        requested_caps = dict(section_caps or {})
        unknown_caps = set(requested_caps) - set(DEFAULT_SECTION_CAPS)
        if unknown_caps:
            names = ", ".join(sorted(unknown_caps))
            raise ValueError(f"unknown fixed context section caps: {names}")
        self.section_caps = {
            key: int(requested_caps.get(key, default))
            for key, default in DEFAULT_SECTION_CAPS.items()
        }
        if any(cap < 0 for cap in self.section_caps.values()):
            raise ValueError("context section caps must be non-negative")
        model = str(getattr(getattr(agent, "model_client", None), "model", ""))
        self.tokenizer = Tokenizer(model)
        self._last_history_metadata = {}
        isolated_client = getattr(agent.model_client, "new_isolated_client", None)
        self.semantic_summarizer = (
            CompactionSummarizer(isolated_client)
            if callable(isolated_client)
            else None
        )

    def build(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        compaction_metadata=None,
        history_override=None,
        action_tools=None,
    ):
        """Build model input without refreshing or writing Runtime state."""
        output_reserve = int(self.agent.config.max_new_tokens)
        instructions_tokens = self.tokenizer.count(self.agent.prompt.instructions)
        tool_schema_tokens = self._tool_schema_tokens(action_tools=action_tools)
        request_overhead_tokens = instructions_tokens + tool_schema_tokens
        raw = self._raw_sections(user_message, history_override=history_override)
        available = self.total_budget - output_reserve - request_overhead_tokens
        minimum_input = self._assemble_input(raw, {})
        if self.tokenizer.count(minimum_input) > available:
            raise ContextBudgetExceeded(
                "runtime policy, repository instructions, and task request "
                "exceed the model budget"
            )

        if history_override is None and self.agent.run.run_log is not None:
            fixed_context = self._fixed_context(raw)
            history_budget = self._history_budget(raw, request_overhead_tokens)
            try:
                compacted_history = (
                    self.agent.run.run_log.render_compacted_projection(
                        retain_tokens=history_budget,
                        token_counter=self._history_token_counter(
                            raw,
                            fixed_context,
                        ),
                    )
                )
            except ValueError as exc:
                raise ContextBudgetExceeded(str(exc)) from exc
            if compacted_history is not None:
                raw["history"], history_metadata = compacted_history
                self._last_history_metadata = dict(history_metadata)

        rendered_context, section_budgets, allocation = self._render_context(
            raw,
            available,
        )
        input_text = self._assemble_input(raw, rendered_context)
        input_text_tokens = self.tokenizer.count(input_text)
        if input_text_tokens > available:
            raise ContextBudgetExceeded(
                "assembled prompt exceeds the available input budget"
            )

        sections = {}
        for key in (
            "runtime_policy",
            "repository_instructions",
            "task_request",
            "latest_user_request",
        ):
            value = raw[key]
            if not value:
                continue
            count = self.tokenizer.count(value)
            sections[key] = {
                "raw_tokens": count,
                "budget_tokens": None,
                "rendered_tokens": count,
            }
        for key, value in rendered_context.items():
            sections[key] = {
                "raw_tokens": self.tokenizer.count(raw[key]),
                "budget_tokens": section_budgets[key],
                "rendered_tokens": self.tokenizer.count(value),
            }
        if rendered_context:
            envelope = self._untrusted_envelope(rendered_context)
            sections["untrusted_context"] = {
                "raw_tokens": self.tokenizer.count(
                    self._untrusted_envelope(
                        {
                            key: raw[key]
                            for key in CONTEXT_WIRE_ORDER
                            if raw[key]
                        }
                    )
                ),
                "budget_tokens": None,
                "rendered_tokens": self.tokenizer.count(envelope),
            }
        prompt_tokens = instructions_tokens + input_text_tokens
        estimated_input_tokens = prompt_tokens + tool_schema_tokens
        metadata = {
            "prompt_tokens": prompt_tokens,
            "instructions_tokens": instructions_tokens,
            "input_text_tokens": input_text_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "reserved_output_tokens": output_reserve,
            "within_budget": (
                estimated_input_tokens + output_reserve <= self.total_budget
            ),
            "tokenizer": self.tokenizer.encoding.name,
            "section_order": [
                "runtime_policy",
                *(
                    ["repository_instructions"]
                    if raw["repository_instructions"]
                    else []
                ),
                "task_request",
                *(["untrusted_context"] if rendered_context else []),
                *(
                    ["latest_user_request"]
                    if raw["latest_user_request"]
                    else []
                ),
            ],
            "sections": sections,
            "included_context_sections": [
                key for key in CONTEXT_WIRE_ORDER if key in rendered_context
            ],
            "budget_allocation": allocation,
            "history_projection": dict(self._last_history_metadata),
            "run_log_generation": int(
                getattr(self.agent.run.run_log, "generation", 0)
            ),
            "compaction": (
                dict(compaction_metadata) if compaction_metadata is not None else None
            ),
            "provider_context_tokens": provider_context_tokens,
        }
        return input_text, metadata

    def prepare_compaction(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        action_tools=None,
    ):
        """Commit semantic compaction or return a bounded read-only fallback."""
        run_log = self.agent.run.run_log
        if run_log is None or run_log.pending_tool_calls():
            return None, None

        raw = self._raw_sections(user_message)
        instructions_tokens = self.tokenizer.count(self.agent.prompt.instructions)
        request_overhead_tokens = (
            instructions_tokens
            + self._tool_schema_tokens(action_tools=action_tools)
        )
        fixed_context = self._fixed_context(raw)
        full_context = dict(fixed_context)
        if raw["history"]:
            full_context["history"] = raw["history"]
        local_context_tokens = (
            self.tokenizer.count(self._assemble_input(raw, full_context))
            + request_overhead_tokens
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
        if context_tokens < threshold_tokens:
            return None, None

        timeout = (
            self.agent.run.execution_context.bounded_timeout()
            if self.agent.run.execution_context is not None
            else None
        )
        failure_code = ""
        failure_detail = ""
        compacted = None
        projection_history_budget = self._history_budget(
            raw,
            request_overhead_tokens,
        )
        history_token_counter = self._history_token_counter(
            raw,
            fixed_context,
        )

        def build_summary(events):
            try:
                summary = self.semantic_summarizer.summarize(
                    events,
                    request_timeout=timeout,
                )
                projected = (
                    "Current run events:\n[compaction] "
                    + summary
                    + "\n"
                    + COMPACTED_HISTORY_OMITTED
                )
                if history_token_counter(projected) > projection_history_budget:
                    raise SemanticCompactionError(
                        "semantic summary does not fit the available History budget"
                    )
                return summary
            except SemanticCompactionError:
                raise
            except Exception as exc:
                raise SemanticCompactionError(
                    f"semantic compaction failed: {type(exc).__name__}: {exc}"
                ) from exc

        try:
            if self.semantic_summarizer is None:
                raise SemanticCompactionError(
                    "model client does not support isolated semantic compaction"
                )
            compacted = run_log.compact(
                retain_tokens=self.compaction_keep_recent_tokens,
                history_token_counter=history_token_counter,
                summary_builder=build_summary,
            )
            if compacted is None:
                failure_code = "semantic_summary_not_committed"
        except SemanticCompactionError as exc:
            failure_code = "semantic_summary_unavailable"
            failure_detail = self.agent.redact_text(str(exc))

        if failure_code:
            history_budget = min(
                self.compaction_keep_recent_tokens,
                projection_history_budget,
            )
            history, projection_metadata = run_log.render_recent_projection(
                retain_tokens=history_budget,
                token_counter=history_token_counter,
            )
            self._last_history_metadata = dict(projection_metadata)
            return (
                {
                    "mode": "runtime_recent_transactions",
                    "degraded": True,
                    "committed": False,
                    "failure_code": failure_code,
                    "failure_detail": failure_detail,
                    "trigger_context_tokens": context_tokens,
                    "local_context_tokens": local_context_tokens,
                    "trigger_threshold_tokens": threshold_tokens,
                    **projection_metadata,
                },
                history,
            )

        event, metadata = compacted
        self.agent.apply_run_event(event)
        semantic_call = (
            self.semantic_summarizer.calls[-1]
            if self.semantic_summarizer.calls
            else {}
        )
        return (
            {
                **metadata,
                "mode": "semantic_history",
                "degraded": False,
                "committed": True,
                "semantic_summary": {
                    "status": "completed",
                    "duration_ms": int(semantic_call.get("duration_ms", 0)),
                    "completion_metadata": dict(
                        semantic_call.get("completion_metadata", {})
                    ),
                },
                "trigger_context_tokens": context_tokens,
                "local_context_tokens": local_context_tokens,
                "trigger_threshold_tokens": threshold_tokens,
            },
            None,
        )

    def _raw_sections(self, user_message, *, history_override=None):
        task = self.agent.run.task
        goal = task.contract.goal if task is not None else str(user_message)
        run_log = self.agent.run.run_log
        latest = run_log.latest_user_guidance() if run_log is not None else ""
        return {
            "runtime_policy": self._runtime_policy_text(),
            "repository_instructions": self._repository_instructions_text(),
            "workspace": self._workspace_text(),
            "repo_map": self._repo_map_text(user_message),
            "working_state": self._working_state_text(),
            "history": (
                str(history_override)
                if history_override is not None
                else self._history_text()
            ),
            "task_request": "task_request:\n"
            + json.dumps(goal, ensure_ascii=False),
            "latest_user_request": (
                "latest_user_request:\n"
                + json.dumps(latest, ensure_ascii=False)
                if latest
                else ""
            ),
        }

    def _tool_schema_tokens(self, *, action_tools=None):
        estimator = getattr(
            self.agent.model_client,
            "estimate_action_tool_tokens",
            None,
        )
        if estimator is None:
            return 0
        selected_tools = (
            self.agent.tools.model_action_tools()
            if action_tools is None
            else action_tools
        )
        return max(
            0,
            int(
                estimator(
                    selected_tools,
                    self.tokenizer.count,
                )
            ),
        )

    def _render_context(self, raw, available):
        rendered = {}
        budgets = {}
        clipped = []

        for section in CONTEXT_ALLOCATION_ORDER:
            text = raw[section]
            if not text:
                continue
            if section == "history":
                remaining = max(
                    0,
                    available
                    - self.tokenizer.count(self._assemble_input(raw, rendered)),
                )
                value = self._bounded_history(
                    text,
                    remaining,
                    token_counter=self._history_token_counter(raw, rendered),
                )
                budget = remaining
            else:
                budget = self.section_caps[section]
                value = self._clip_complete_lines(
                    text,
                    budget,
                    token_counter=self._context_section_token_counter(
                        raw,
                        rendered,
                        section,
                    ),
                )
            if not value:
                clipped.append(section)
                continue
            candidate = {**rendered, section: value}
            if self.tokenizer.count(self._assemble_input(raw, candidate)) > available:
                remaining = max(
                    0,
                    available
                    - self.tokenizer.count(self._assemble_input(raw, rendered)),
                )
                value = (
                    self._bounded_history(
                        text,
                        remaining,
                        token_counter=self._history_token_counter(raw, rendered),
                    )
                    if section == "history"
                    else self._clip_complete_lines(
                        text,
                        remaining,
                        token_counter=self._context_section_token_counter(
                            raw,
                            rendered,
                            section,
                        ),
                    )
                )
                budget = remaining
                candidate = {**rendered, section: value} if value else rendered
            if (
                value
                and self.tokenizer.count(self._assemble_input(raw, candidate))
                <= available
            ):
                rendered[section] = value
                budgets[section] = budget
                if value != text:
                    clipped.append(section)
            else:
                clipped.append(section)

        input_text = self._assemble_input(raw, rendered)
        return rendered, budgets, {
            "strategy": "fixed_caps_history_remainder",
            "available_input_tokens": available,
            "history_budget_tokens": budgets.get("history", 0),
            "unused_input_tokens": max(
                0, available - self.tokenizer.count(input_text)
            ),
            "clipped_sections": list(dict.fromkeys(clipped)),
        }

    def _fixed_context(self, raw):
        rendered = {}
        for section in FIXED_SECTION_ALLOCATION_ORDER:
            if not raw[section]:
                continue
            value = self._clip_complete_lines(
                raw[section],
                self.section_caps[section],
                token_counter=self._context_section_token_counter(
                    raw,
                    rendered,
                    section,
                ),
            )
            if value:
                rendered[section] = value
        return rendered

    def _clip_complete_lines(self, text, limit, *, token_counter):
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

    def _bounded_history(self, text, limit, *, token_counter):
        text = str(text).strip()
        limit = max(0, int(limit))
        if not text or limit <= 0:
            return ""
        if token_counter(text) <= limit:
            return text
        run_log = self.agent.run.run_log
        if run_log is None:
            return ""
        bounded, metadata = run_log.render_recent_projection(
            retain_tokens=limit,
            token_counter=token_counter,
        )
        if token_counter(bounded) > limit:
            return ""
        self._last_history_metadata = dict(metadata)
        return bounded

    def _untrusted_envelope(self, context):
        lines = ['<untrusted_context trust="untrusted_data">']
        for section in CONTEXT_WIRE_ORDER:
            value = str(context.get(section, "")).strip()
            if not value:
                continue
            lines.extend(
                (
                    f'<section name="{section}">',
                    escape(value, quote=False),
                    "</section>",
                )
            )
        lines.append("</untrusted_context>")
        return "\n".join(lines)

    def _context_section_token_counter(self, raw, context, section):
        base_context = {
            key: value
            for key, value in context.items()
            if key != section
        }
        base_tokens = self.tokenizer.count(
            self._assemble_input(raw, base_context)
        )

        def count(text):
            candidate = {**base_context, section: str(text)}
            return max(
                0,
                self.tokenizer.count(
                    self._assemble_input(raw, candidate)
                )
                - base_tokens,
            )

        return count

    def _history_token_counter(self, raw, context):
        return self._context_section_token_counter(
            {**raw, "history": ""},
            context,
            "history",
        )

    def _assemble_input(self, raw, context):
        parts = [raw["runtime_policy"]]
        if raw["repository_instructions"]:
            parts.append(raw["repository_instructions"])
        parts.append(raw["task_request"])
        if context:
            parts.append(self._untrusted_envelope(context))
        if raw["latest_user_request"]:
            parts.append(raw["latest_user_request"])
        return "\n\n".join(parts)

    def _runtime_policy_text(self):
        state = self.agent.run.task
        if state is None:
            policy = {
                "mode": "unavailable",
                "verify_changes": False,
                "write_scope": {"mode": "unavailable"},
            }
        else:
            contract = state.contract
            mode = (
                "ask"
                if self.agent.config.mode == "ask"
                or not contract.allows_workspace_mutation
                else self.agent.config.mode
            )
            if mode == "ask":
                write_scope = {"mode": "none"}
            elif contract.allowed_write_paths is None:
                write_scope = {"mode": "workspace"}
            else:
                write_scope = {
                    "mode": "paths",
                    "paths": list(contract.allowed_write_paths),
                }
            policy = {
                "mode": mode,
                "verify_changes": contract.verify_changes,
                "write_scope": write_scope,
            }
        return "runtime_policy:\n" + json.dumps(
            policy,
            ensure_ascii=False,
            sort_keys=True,
        )

    def _workspace_text(self):
        return self.agent.workspace.text()

    def _repository_instructions_text(self):
        instructions = self.agent.prompt.repository_instructions
        if not instructions:
            return ""
        lines = ["<repository_instructions>"]
        for path, content in instructions.items():
            lines.extend(
                (
                    f'<instructions path="{escape(path, quote=True)}">',
                    escape(content, quote=False),
                    "</instructions>",
                )
            )
        lines.append("</repository_instructions>")
        return "\n".join(lines)

    def _working_state_text(self):
        task_state = self.agent.run.task
        working = task_state.working if task_state is not None else WorkingState()
        lines = []
        for label, values in (
            ("constraints", working.constraints),
            ("decisions", working.decisions),
            ("next_steps", working.next_steps),
        ):
            if values:
                lines.append(label + ":")
                lines.extend(f"- {value}" for value in values)
        return "\n".join(lines)

    def _repo_map_text(self, query):
        task = self.agent.run.task
        evidence = self.agent.run.evidence
        parts = []
        if task is not None and task.contract.goal:
            parts.append("Task goal:\n" + task.contract.goal)
        query = str(query).strip()
        if query:
            parts.append("Current request:\n" + query)
        working_state = self._working_state_text()
        if working_state:
            parts.append("Current working state:\n" + working_state)
        active_paths = {
            str(item.get("path", "")).strip()
            for item in evidence.observations
            if item.get("status") == "success" and item.get("path")
        }
        active_paths.update(evidence.touched_paths)
        if active_paths:
            parts.append(
                "Active paths:\n"
                + "\n".join(f"- {path}" for path in sorted(active_paths))
            )
        result = self.agent.dependencies.repo_map.render(
            "\n\n".join(parts),
            budget_tokens=self.section_caps["repo_map"],
            max_results=24,
            token_counter=self.tokenizer.count,
        )
        return result.text if result.details.get("selected_count", 0) else ""

    def _history_text(self):
        run_log = self.agent.run.run_log
        if run_log is None:
            self._last_history_metadata = {
                "active_count": 0,
                "selected_count": 0,
                "omitted_count": 0,
                "artifact_references": 0,
            }
            return ""
        history, metadata = run_log.render_projection()
        self._last_history_metadata = metadata
        return history if metadata.get("selected_count", 0) else ""

    def _history_budget(self, raw, request_overhead_tokens):
        available = (
            self.total_budget
            - int(self.agent.config.max_new_tokens)
            - max(0, int(request_overhead_tokens or 0))
        )
        empty_history = {**raw, "history": ""}
        fixed_context = self._fixed_context(raw)
        minimum = self._assemble_input(empty_history, fixed_context)
        remaining = available - self.tokenizer.count(minimum)
        return max(0, remaining)
