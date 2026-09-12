"""Build stable Responses instructions plus dynamic model input."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import context_manager as context
from .compaction_summary import CompactionSummarizer, SemanticCompactionError
from .execution import (
    ExecutionCancelled,
    ExecutionContext,
    ExecutionDeadlineExceeded,
)
from .history import HISTORY_OMITTED
from .prompt_instructions import build_prompt_instructions
from .verification import ResolvedVerificationPolicy
from .workspace import WORKSPACE_GIT_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .runtime import Pico
    from .tool_runtime import ResolvedToolSurface

AGENTS_MD_MAX_BYTES = 32 * 1024


def load_repository_instructions(repo_root, cwd, *, access_paths=()):
    """Load scoped rules along startup and accessed paths, without scanning subtrees."""

    repo_root = Path(repo_root).resolve()
    cwd = Path(cwd).resolve()
    directories = {repo_root}
    for target in (cwd, *access_paths):
        target = Path(target).resolve()
        directory = target if target.is_dir() else target.parent
        relative = directory.relative_to(repo_root)
        current = repo_root
        for part in relative.parts:
            current /= part
            directories.add(current)

    instructions = {}
    remaining = AGENTS_MD_MAX_BYTES
    for directory in sorted(directories, key=lambda path: (len(path.parts), path.as_posix())):
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
    """Own prompt construction and compaction planning."""

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

    def refresh_repository_instructions(self):
        paths = []
        log = self.runtime.run.run_log
        for event in log.events if log is not None else ():
            if event.kind not in {"tool_exchange", "tool_intent"}:
                continue
            call = event.tool_call
            if call.name not in {"list_files", "search", "read_file", "write_file", "edit_file"}:
                continue
            raw = call.args.get("path", ".")
            if not isinstance(raw, str):
                continue
            try:
                paths.append(self.runtime.workspace.resolve_tool_path(raw))
            except ValueError:
                continue  # Invalid paths remain the ToolRuntime's responsibility.
        current = load_repository_instructions(
            self.runtime.workspace.root, self.runtime.workspace.cwd, access_paths=paths
        )
        changed = current != self.repository_instructions
        self.repository_instructions = current
        return changed

    def prepare(self, user_message, *, tool_surface: ResolvedToolSurface):
        """Sample context and calculate budgets once for one prompt rebuild."""
        raw = self._raw_sections(user_message, tool_surface)
        instructions_tokens = self.count_tokens(self.instructions)
        tool_schema_tokens = self._tool_schema_tokens(tool_surface)
        available = (self.runtime.config.provider_context_limit_tokens
                     - self.runtime.config.max_new_tokens - instructions_tokens - tool_schema_tokens)
        fixed = context._fixed_context(raw, section_caps=self.section_caps, count_tokens=self.count_tokens)
        return {
            "raw": raw,
            "instructions_tokens": instructions_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "available": available,
            "fixed_context": fixed,
            "history_budget": context._history_budget(
                raw, available, fixed_context=fixed, count_tokens=self.count_tokens
            ),
            "history_token_counter": context._history_token_counter(raw, fixed, count_tokens=self.count_tokens),
        }

    def build(self, inputs, *, history_override=None):
        """Render prepared context with the current, possibly compacted history."""
        raw = inputs["raw"]
        history = self._history()
        count_tokens = self.count_tokens
        available = inputs["available"]
        history_text = (
            context.render_history(history)
            if history_override is None
            else history_override
        )
        raw = {**raw, "history": history_text}
        minimum_input = context._assemble_input(raw, context._required_context(raw))
        if count_tokens(minimum_input) > available:
            raise context.ContextBudgetExceeded(
                "runtime policy, repository instructions, task request, pending "
                "Runtime instruction and evidence exceed the model budget"
            )
        if history_override is None and history is not None:
            try:
                compacted_history = history.render_compacted_projection(
                    retain_tokens=inputs["history_budget"],
                    token_counter=inputs["history_token_counter"],
                )
            except ValueError as exc:
                raise context.ContextBudgetExceeded(str(exc)) from exc
            if compacted_history is not None:
                raw["history"] = compacted_history
        rendered_context = context._render_context(
            raw,
            available,
            section_caps=self.section_caps,
            count_tokens=count_tokens,
            history=history,
        )
        input_text = context._assemble_input(raw, rendered_context)
        if count_tokens(input_text) > available:
            raise context.ContextBudgetExceeded(
                "assembled prompt exceeds the available input budget"
            )
        return ModelPrompt(self.instructions, input_text)

    def plan_compaction(self, inputs, *, provider_context_tokens=None):
        """Plan semantic compaction or return a bounded read-only fallback."""
        run_log = self.runtime.run.run_log
        if run_log is None or run_log.pending_tool_call() is not None:
            return None, None
        history = self._history()
        config = self.runtime.config
        count_tokens = self.count_tokens
        history_text = context.render_history(history)
        raw = {**inputs["raw"], "history": history_text}
        request_overhead_tokens = inputs["instructions_tokens"] + inputs["tool_schema_tokens"]
        fixed_context = inputs["fixed_context"]
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
            return None, None
        projection_history_budget = inputs["history_budget"]
        history_token_counter = inputs["history_token_counter"]

        def build_summary(events, *, max_summary_tokens):
            try:
                summary = self.semantic_summarizer.summarize(
                    events,
                    task_goal=(
                        self.runtime.run.projection.contract.goal
                        if self.runtime.run.projection.contract is not None
                        else ""
                    ),
                    execution_context=self.runtime.run.execution_context,
                    context_limit_tokens=config.provider_context_limit_tokens,
                    max_output_tokens=min(
                        config.summary_max_output_tokens, max_summary_tokens
                    ),
                    count_tokens=count_tokens,
                )
                projected = (
                    "Current run events:\n[compaction] "
                    + summary
                    + "\n"
                    + HISTORY_OMITTED
                )
                if history_token_counter(projected) > projection_history_budget:
                    raise SemanticCompactionError(
                        "semantic summary does not fit the available History budget"
                    )
                return summary
            except SemanticCompactionError:
                raise
            except (ExecutionCancelled, ExecutionDeadlineExceeded):
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
                max_history_tokens=projection_history_budget,
                history_token_counter=history_token_counter,
                summary_builder=build_summary,
            )
            if compacted is None:
                raise SemanticCompactionError("summary did not reduce history")
        except SemanticCompactionError:
            history_budget = min(
                config.compaction_keep_recent_tokens, projection_history_budget
            )
            fallback_history = history.render_recent_projection(
                retain_tokens=history_budget, token_counter=history_token_counter
            )
            return None, fallback_history
        summary, covered = compacted
        return (summary, covered), None

    def _history(self):
        run_log = self.runtime.run.run_log
        return (
            run_log.history()
            if run_log is not None
            else None
        )

    def _tool_schema_tokens(self, tool_surface):
        estimator = getattr(
            self.runtime.model_client, "estimate_action_tool_tokens", None
        )
        if estimator is None:
            return 0
        return max(
            0,
            int(estimator(tool_surface.action_tools, self.count_tokens)),
        )

    def _raw_sections(self, user_message, tool_surface):
        self.refresh_repository_instructions()
        projection = self.runtime.run.projection
        contract = projection.contract
        goal = contract.goal if contract is not None else str(user_message)
        history = self._history()
        latest = history.latest_user_guidance() if history is not None else ""
        feedback = projection.runtime_feedback
        verification_policy = (
            ResolvedVerificationPolicy.resolve(
                contract,
                self.runtime.config.verification_command,
            )
            if contract is not None
            else None
        )
        return {
            "runtime_policy": context.render_runtime_policy(
                contract,
                tool_surface.mode,
                tool_surface.allowed_write_paths,
                bool(
                    verification_policy
                    and verification_policy.verify_net_changes
                ),
            ),
            "repository_instructions": context.render_repository_instructions(
                self.repository_instructions
            ),
            "workspace": self.runtime.workspace.text(
                command_runner=self.runtime.dependencies.command_runner,
                execution_context=(
                    self.runtime.run.execution_context
                    or ExecutionContext.standalone(
                        max_seconds=WORKSPACE_GIT_TIMEOUT_SECONDS
                    )
                ),
            ),
            "task_request": "task_request:\n" + json.dumps(goal, ensure_ascii=False),
            **context.runtime_feedback_sections(feedback),
            "latest_user_request": (
                "latest_user_request:\n" + json.dumps(latest, ensure_ascii=False)
                if latest
                else ""
            ),
        }
