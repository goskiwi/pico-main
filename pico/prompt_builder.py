"""Build stable Responses instructions plus dynamic model input."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import context_manager as context
from .compaction_summary import CompactionSummarizer, SemanticCompactionError
from .history import COMPACTED_HISTORY_OMITTED, RunHistory
from .prompt_instructions import build_prompt_instructions
from .working_state import WorkingState

if TYPE_CHECKING:
    from .runtime import Pico

AGENTS_MD_MAX_BYTES = 32 * 1024


def load_repository_instructions(repo_root, cwd):
    """Load applicable AGENTS.md files once from repository root through CWD."""

    repo_root = Path(repo_root).resolve()
    cwd = Path(cwd).resolve()
    relative = cwd.relative_to(repo_root)
    directories = [repo_root]
    current = repo_root
    for part in relative.parts:
        current /= part
        directories.append(current)

    instructions = {}
    remaining = AGENTS_MD_MAX_BYTES
    for directory in directories:
        path = directory / "AGENTS.md"
        if remaining <= 0 or not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        selected = raw[:remaining]
        content = selected.decode("utf-8", errors="replace")
        if len(selected) < len(raw):
            content += "\n...[repository instructions truncated]"
        instructions[path.relative_to(repo_root).as_posix()] = content
        remaining -= len(selected)
    return instructions


@dataclass(frozen=True)
class ModelPrompt:
    instructions: str
    input_text: str


class PromptBuilder:
    """Own prompt construction and compaction planning; delegate only text and budget helpers."""

    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.instructions = build_prompt_instructions()
        self.repository_instructions = load_repository_instructions(
            runtime.workspace.root, runtime.workspace.cwd
        )
        self.tokenizer = context.Tokenizer(getattr(runtime.model_client, "model", ""))
        self.section_caps = dict(context.DEFAULT_SECTION_CAPS)
        factory = getattr(runtime.model_client, "new_isolated_client", None)
        self.semantic_summarizer = (
            CompactionSummarizer(factory) if callable(factory) else None
        )

    def count_tokens(self, text):
        return self.tokenizer.count(text)

    def build(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        compaction_metadata=None,
        history_override=None,
        action_tools=None,
    ):
        """Build one prompt and its diagnostics from current Runtime inputs."""
        raw = self._raw_sections(user_message)
        history = self._history()
        config = self.runtime.config
        instructions_tokens = self.count_tokens(self.instructions)
        tool_schema_tokens = self._tool_schema_tokens(action_tools)
        run_log = self.runtime.run.run_log
        run_log_generation = run_log.generation if run_log is not None else 0
        count_tokens = self.tokenizer.count
        output_reserve = int(config.max_new_tokens)
        request_overhead_tokens = instructions_tokens + tool_schema_tokens
        (history_text, history_metadata) = (
            context.render_history(history)
            if history_override is None
            else history_override
        )
        raw = {**raw, "history": history_text}
        available = (
            config.provider_context_limit_tokens
            - output_reserve
            - request_overhead_tokens
        )
        minimum_input = context._assemble_input(raw, context._required_context(raw))
        if count_tokens(minimum_input) > available:
            raise context.ContextBudgetExceeded(
                "runtime policy, repository instructions, task request, pending "
                "Runtime instruction and WorkingState exceed the model budget"
            )
        if history_override is None and history is not None:
            fixed_context = context._fixed_context(
                raw, section_caps=self.section_caps, count_tokens=count_tokens
            )
            history_budget = context._history_budget(
                raw,
                available,
                section_caps=self.section_caps,
                count_tokens=count_tokens,
            )
            try:
                compacted_history = history.render_compacted_projection(
                    retain_tokens=history_budget,
                    token_counter=context._history_token_counter(
                        raw, fixed_context, count_tokens=count_tokens
                    ),
                )
            except ValueError as exc:
                raise context.ContextBudgetExceeded(str(exc)) from exc
            if compacted_history is not None:
                (raw["history"], history_metadata) = compacted_history
        (rendered_context, section_budgets, allocation, bounded_metadata) = (
            context._render_context(
                raw,
                available,
                section_caps=self.section_caps,
                count_tokens=count_tokens,
                history=history,
            )
        )
        if bounded_metadata is not None:
            history_metadata = bounded_metadata
        input_text = context._assemble_input(raw, rendered_context)
        input_text_tokens = count_tokens(input_text)
        if input_text_tokens > available:
            raise context.ContextBudgetExceeded(
                "assembled prompt exceeds the available input budget"
            )
        sections = {}
        for key in (
            "runtime_policy",
            "repository_instructions",
            "task_request",
            "runtime_instruction",
            "latest_user_request",
        ):
            value = raw[key]
            if not value:
                continue
            count = count_tokens(value)
            sections[key] = {
                "raw_tokens": count,
                "budget_tokens": None,
                "rendered_tokens": count,
            }
        for key, value in rendered_context.items():
            sections[key] = {
                "raw_tokens": count_tokens(raw[key]),
                "budget_tokens": section_budgets[key],
                "rendered_tokens": count_tokens(value),
            }
        if rendered_context:
            envelope = context._untrusted_envelope(rendered_context)
            sections["untrusted_context"] = {
                "raw_tokens": count_tokens(
                    context._untrusted_envelope(
                        {
                            key: raw[key]
                            for key in context.CONTEXT_WIRE_ORDER
                            if raw[key]
                        }
                    )
                ),
                "budget_tokens": None,
                "rendered_tokens": count_tokens(envelope),
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
            "within_budget": estimated_input_tokens + output_reserve
            <= config.provider_context_limit_tokens,
            "tokenizer": self.tokenizer.encoding.name,
            "section_order": [
                "runtime_policy",
                *(
                    ["repository_instructions"]
                    if raw["repository_instructions"]
                    else []
                ),
                "task_request",
                *(
                    ["runtime_instruction"]
                    if raw["runtime_instruction"]
                    else []
                ),
                *(["untrusted_context"] if rendered_context else []),
                *(["latest_user_request"] if raw["latest_user_request"] else []),
            ],
            "sections": sections,
            "included_context_sections": [
                key for key in context.CONTEXT_WIRE_ORDER if key in rendered_context
            ],
            "budget_allocation": allocation,
            "history_projection": dict(history_metadata),
            "run_log_generation": run_log_generation,
            "compaction": dict(compaction_metadata)
            if compaction_metadata is not None
            else None,
            "provider_context_tokens": provider_context_tokens,
        }
        return (ModelPrompt(self.instructions, input_text), metadata)

    def plan_compaction(
        self, user_message, *, provider_context_tokens=None, action_tools=None
    ):
        """Plan semantic compaction or return a bounded read-only fallback."""
        run_log = self.runtime.run.run_log
        if run_log is None or run_log.pending_tool_calls():
            return (None, None, None)
        raw = self._raw_sections(user_message)
        history = self._history()
        config = self.runtime.config
        instructions_tokens = self.count_tokens(self.instructions)
        tool_schema_tokens = self._tool_schema_tokens(action_tools)
        count_tokens = self.tokenizer.count
        (history_text, _metadata) = context.render_history(history)
        raw = {**raw, "history": history_text}
        request_overhead_tokens = instructions_tokens + tool_schema_tokens
        fixed_context = context._fixed_context(
            raw, section_caps=self.section_caps, count_tokens=count_tokens
        )
        full_context = dict(fixed_context)
        if raw["history"]:
            full_context["history"] = raw["history"]
        local_context_tokens = (
            count_tokens(context._assemble_input(raw, full_context))
            + request_overhead_tokens
        )
        context_tokens = max(local_context_tokens, int(provider_context_tokens or 0))
        reserve_tokens = max(
            int(config.max_new_tokens), config.compaction_reserve_tokens
        )
        threshold_tokens = max(1, config.provider_context_limit_tokens - reserve_tokens)
        if context_tokens < threshold_tokens:
            return (None, None, None)
        timeout = (
            self.runtime.run.execution_context.bounded_timeout()
            if self.runtime.run.execution_context is not None
            else None
        )
        failure_code = ""
        failure_detail = ""
        compacted = None
        available = (
            config.provider_context_limit_tokens
            - int(config.max_new_tokens)
            - request_overhead_tokens
        )
        projection_history_budget = context._history_budget(
            raw, available, section_caps=self.section_caps, count_tokens=count_tokens
        )
        history_token_counter = context._history_token_counter(
            raw, fixed_context, count_tokens=count_tokens
        )

        def build_summary(events):
            try:
                summary = self.semantic_summarizer.summarize(
                    events, request_timeout=timeout
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
            compacted = history.plan_compaction(
                retain_tokens=config.compaction_keep_recent_tokens,
                history_token_counter=history_token_counter,
                summary_builder=build_summary,
            )
            if compacted is None:
                failure_code = "semantic_summary_not_committed"
        except SemanticCompactionError as exc:
            failure_code = "semantic_summary_unavailable"
            failure_detail = self.runtime.redact_text(str(exc))
        if failure_code:
            history_budget = min(
                config.compaction_keep_recent_tokens, projection_history_budget
            )
            fallback_history = history.render_recent_projection(
                retain_tokens=history_budget, token_counter=history_token_counter
            )
            (_text, projection_metadata) = fallback_history
            return (
                None,
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
                fallback_history,
            )
        (summary, covered, metadata) = compacted
        semantic_call = (
            self.semantic_summarizer.calls[-1] if self.semantic_summarizer.calls else {}
        )
        return (
            (summary, covered),
            {
                **metadata,
                "mode": "semantic_history",
                "degraded": False,
                "committed": False,
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

    def _history(self):
        run_log = self.runtime.run.run_log
        return (
            RunHistory(
                run_log.events,
                history_projectors=self.runtime.tools.history_projectors(),
            )
            if run_log is not None
            else None
        )

    def _tool_schema_tokens(self, action_tools=None):
        estimator = getattr(
            self.runtime.model_client, "estimate_action_tool_tokens", None
        )
        if estimator is None:
            return 0
        tools = (
            self.runtime.tools.model_action_tools()
            if action_tools is None
            else action_tools
        )
        return max(0, int(estimator(tools, self.count_tokens)))

    def _raw_sections(self, user_message):
        projection = self.runtime.run.projection
        contract = projection.contract
        goal = contract.goal if contract is not None else str(user_message)
        history = self._history()
        latest = history.latest_user_guidance() if history is not None else ""
        working = projection.working if contract is not None else WorkingState()
        working_text = context.render_working_state(working)
        mode, paths = self.runtime.tools.effective_policy()
        return {
            "runtime_policy": context.render_runtime_policy(contract, mode, paths),
            "repository_instructions": context.render_repository_instructions(
                self.repository_instructions
            ),
            "workspace": self.runtime.workspace.text(),
            "repo_map": self._repo_map_text(user_message, working_text),
            "working_state": working_text,
            "task_request": "task_request:\n" + json.dumps(goal, ensure_ascii=False),
            "runtime_instruction": (
                "runtime_instruction:\n"
                + json.dumps(
                    projection.pending_runtime_instruction,
                    ensure_ascii=False,
                )
                if projection.pending_runtime_instruction
                else ""
            ),
            "latest_user_request": (
                "latest_user_request:\n" + json.dumps(latest, ensure_ascii=False)
                if latest
                else ""
            ),
        }

    def _repo_map_text(self, query, working_text):
        if not self.runtime.config.repo_map_enabled:
            return ""
        contract = self.runtime.run.projection.contract
        evidence = self.runtime.run.evidence
        parts = []
        if contract is not None and contract.goal:
            parts.append("Task goal:\n" + contract.goal)
        query = str(query).strip()
        if query:
            parts.append("Current request:\n" + query)
        if working_text:
            parts.append("Current working state:\n" + working_text)
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
        result = self.runtime.dependencies.repo_map.render(
            "\n\n".join(parts),
            budget_tokens=self.section_caps["repo_map"],
            max_results=24,
            token_counter=self.count_tokens,
        )
        return result.text if result.details.get("selected_count", 0) else ""
