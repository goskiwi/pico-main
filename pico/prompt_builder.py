"""Build stable Responses instructions plus dynamic model input."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from . import context_manager as context
from .compaction_summary import (
    SUMMARY_MAX_OUTPUT_TOKENS,
    CompactionSummarizer,
    SemanticCompactionError,
)
from .execution import (
    ExecutionCancelled,
    ExecutionContext,
    ExecutionDeadlineExceeded,
)
from .failure_policy import guidance_for_failure
from .prompt_instructions import build_prompt_instructions
from .workspace import WORKSPACE_GIT_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from .runtime import Pico
    from .tool_runtime import ResolvedToolSurface

AGENTS_MD_MAX_BYTES = 32 * 1024
CONTEXT_WIRE_ORDER = ("workspace", "history")


def load_project_instructions(repo_root):
    """Load only the workspace-root AGENTS.md instruction file."""
    repo_root = Path(repo_root).resolve()
    path = repo_root / "AGENTS.md"
    if not path.is_file() or path.is_symlink():
        return {}
    raw = path.read_bytes()
    selected = raw[:AGENTS_MD_MAX_BYTES]
    content = selected.decode("utf-8", errors="replace")
    if len(selected) < len(raw):
        content += "\n...[repository instructions truncated]"
    return {"AGENTS.md": content}


def _context_sections(selected):
    tags = {
        "workspace": "environment_context",
        "history": "conversation_history",
    }
    sections = []
    for name in CONTEXT_WIRE_ORDER:
        value = str(selected.get(name, "")).strip()
        if not value:
            continue
        tag = tags[name]
        sections.append(
            f"<{tag}>\n{escape(value, quote=False)}\n</{tag}>"
        )
    return "\n\n".join(sections)


def _assemble_input(raw, selected):
    parts = [raw["permissions"]]
    if raw["project_instructions"]:
        parts.append(raw["project_instructions"])
    if selected.get("user_messages"):
        parts.append(selected["user_messages"])
    if raw["retry_instruction"]:
        parts.append(raw["retry_instruction"])
    if selected:
        context_sections = _context_sections(selected)
        if context_sections:
            parts.append(context_sections)
    return "\n\n".join(parts)


def render_retry_instruction(guidance):
    guidance = str(guidance).strip()
    if not guidance:
        return ""
    return "retry_instruction:\n" + json.dumps(
        guidance,
        ensure_ascii=False,
    )


def render_user_messages(goal, later_messages=()):
    return "user_messages:\n" + json.dumps(
        [str(goal), *(str(message) for message in later_messages)],
        ensure_ascii=False,
    )


def render_permissions(contract, mode, paths):
    if contract is None:
        policy = {
            "mode": "unavailable",
            "write_scope": {"mode": "unavailable"},
        }
    else:
        if mode == "ask":
            write_scope = {"mode": "none"}
        elif paths is None:
            write_scope = {"mode": "workspace"}
        else:
            write_scope = {"mode": "paths", "paths": list(paths)}
        policy = {
            "mode": mode,
            "write_scope": write_scope,
        }
    return "permissions:\n" + json.dumps(
        policy, ensure_ascii=False, sort_keys=True
    )


def render_project_instructions(instructions):
    if not instructions:
        return ""
    content = instructions["AGENTS.md"]
    return "\n".join(
        (
            "<project_instructions>",
            '<instructions path="AGENTS.md">',
            "Applies to the entire workspace.",
            escape(content, quote=False),
            "</instructions>",
            "</project_instructions>",
        )
    )


def render_history(history):
    return "" if history is None else history.render_projection()


@dataclass(frozen=True)
class ModelPrompt:
    instructions: str
    input_text: str


class PromptBuilder:
    """Own prompt construction and compaction planning."""

    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.instructions = build_prompt_instructions()
        self.project_instructions = self._load_project_instructions()
        self.tokenizer = context.Tokenizer(getattr(runtime.model_client, "model", ""))
        self.section_caps = dict(context.DEFAULT_SECTION_CAPS)
        factory = getattr(runtime.model_client, "new_isolated_client", None)
        self.semantic_summarizer = (
            CompactionSummarizer(factory) if callable(factory) else None
        )

    def count_tokens(self, text):
        return self.tokenizer.count(text)

    def _load_project_instructions(self):
        return {
            path: self.runtime.redact_text(content)
            for path, content in load_project_instructions(
                self.runtime.workspace.root
            ).items()
        }

    def build_for_run(self, *, tool_surface, provider_context_tokens=None):
        """Prepare, optionally compact durably, then render one model input."""
        inputs = self.prepare(tool_surface=tool_surface)
        plan = self.plan_compaction(inputs, provider_context_tokens=provider_context_tokens)
        if plan is not None:
            self.runtime.run.run_log.append_compaction(plan)
            self.runtime.dependencies.run_store.checkpoint_if_due(
                self.runtime.run.run_log,
                force=True,
            )
        return self.build(inputs, refresh_history=plan is not None)

    def refresh_project_instructions(self):
        current = self._load_project_instructions()
        changed = current != self.project_instructions
        self.project_instructions = current
        return changed

    def prepare(self, *, tool_surface: ResolvedToolSurface):
        """Sample context and calculate budgets once for one prompt rebuild."""
        history = self._history()
        history_text = render_history(history)
        raw = self._raw_sections(tool_surface, history=history)
        instructions_tokens = self.count_tokens(self.instructions)
        tool_schema_tokens = self._tool_schema_tokens(tool_surface)
        available = (
            self.runtime.effective_context_limit_tokens
            - self.runtime.config.max_output_tokens
            - instructions_tokens
            - tool_schema_tokens
        )
        fixed = context.fixed_context(
            raw,
            section_caps=self.section_caps,
            count_tokens=self.count_tokens,
            render_input=_assemble_input,
            history=history,
        )
        return {
            "raw": raw,
            "instructions_tokens": instructions_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "available": available,
            "fixed_context": fixed,
            "history": history,
            "history_text": history_text,
        }

    def build(self, inputs, *, refresh_history=False):
        """Render one prompt after Context performs the sole History selection."""
        raw = inputs["raw"]
        history = self._history() if refresh_history else inputs["history"]
        count_tokens = self.count_tokens
        available = inputs["available"]
        history_text = (
            render_history(history) if refresh_history else inputs["history_text"]
        )
        raw = {
            **raw,
            "history": history_text,
            "user_messages": (
                render_user_messages(
                    self.runtime.run.projection.contract.goal,
                    history.user_texts(),
                )
                if refresh_history and history is not None
                else raw["user_messages"]
            ),
        }
        minimum_input = _assemble_input(raw, context.required_context(raw))
        if count_tokens(minimum_input) > available:
            raise context.ContextBudgetExceeded(
                "permissions, project instructions, user messages and retry "
                "instruction exceed the model budget"
            )
        rendered_context = context.select_context(
            raw,
            available,
            section_caps=self.section_caps,
            count_tokens=count_tokens,
            history=history,
            render_input=_assemble_input,
        )
        input_text = _assemble_input(raw, rendered_context)
        if count_tokens(input_text) > available:
            raise context.ContextBudgetExceeded(
                "assembled prompt exceeds the available input budget"
            )
        return ModelPrompt(self.instructions, input_text)

    def plan_compaction(self, inputs, *, provider_context_tokens=None):
        """Plan semantic compaction; Context owns every bounded fallback."""
        run_log = self.runtime.run.run_log
        if run_log is None or run_log.projection.pending_tool is not None:
            return None
        history = inputs["history"]
        config = self.runtime.config
        count_tokens = self.count_tokens
        history_text = inputs["history_text"]
        raw = {**inputs["raw"], "history": history_text}
        request_overhead_tokens = inputs["instructions_tokens"] + inputs["tool_schema_tokens"]
        fixed_context = inputs["fixed_context"]
        full_context = dict(fixed_context)
        if raw["history"]:
            full_context["history"] = raw["history"]
        local_context_tokens = (
            count_tokens(_assemble_input(raw, full_context))
            + request_overhead_tokens
        )
        context_tokens = max(local_context_tokens, int(provider_context_tokens or 0))
        threshold_tokens = max(
            1,
            self.runtime.effective_context_limit_tokens - config.max_output_tokens,
        )
        if context_tokens < threshold_tokens:
            return None
        def build_summary(events, *, max_summary_tokens):
            try:
                return self.semantic_summarizer.summarize(
                    events,
                    task_goal=(
                        self.runtime.run.projection.contract.goal
                        if self.runtime.run.projection.contract is not None
                        else ""
                    ),
                    execution_context=self.runtime.run.execution_context,
                    effective_context_limit_tokens=(
                        self.runtime.effective_context_limit_tokens
                    ),
                    max_output_tokens=min(
                        SUMMARY_MAX_OUTPUT_TOKENS,
                        config.max_output_tokens,
                        max_summary_tokens,
                    ),
                    count_tokens=count_tokens,
                )
            except SemanticCompactionError:
                raise
            except (ExecutionCancelled, ExecutionDeadlineExceeded):
                raise
            except Exception as exc:
                raise SemanticCompactionError(
                    f"semantic compaction failed: {type(exc).__name__}: {exc}"
                ) from exc

        if self.semantic_summarizer is None:
            raise SemanticCompactionError(
                "model client does not support isolated semantic compaction"
            )
        def raw_after(events, history_text=""):
            return {
                **raw,
                "history": history_text,
                "user_messages": render_user_messages(
                    self.runtime.run.projection.contract.goal,
                    history.user_texts(events),
                ),
            }

        def context_budget(events):
            candidate_raw = raw_after(events)
            required = context.required_context(candidate_raw)
            return (
                context.history_budget(
                    candidate_raw,
                    inputs["available"],
                    fixed_context=required,
                    count_tokens=count_tokens,
                    render_input=_assemble_input,
                ),
                context.history_token_counter(
                    candidate_raw,
                    required,
                    count_tokens=count_tokens,
                    render_input=_assemble_input,
                ),
            )

        def context_size(events, history_text):
            candidate_raw = raw_after(events, history_text)
            selected = context.required_context(candidate_raw)
            if history_text:
                selected["history"] = history_text
            return count_tokens(_assemble_input(candidate_raw, selected))

        compacted = history.plan_compaction(
            retain_tokens=config.recent_history_tokens,
            token_counter=count_tokens,
            context_budget=context_budget,
            context_size=context_size,
            summary_builder=build_summary,
        )
        if compacted is None:
            raise SemanticCompactionError("summary did not reduce history")
        return compacted

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

    def _raw_sections(self, tool_surface, *, history):
        projection = self.runtime.run.projection
        contract = projection.contract
        if contract is None:
            raise RuntimeError("Prompt construction requires an active TaskContract")
        goal = contract.goal
        return {
            "permissions": render_permissions(
                contract,
                tool_surface.mode,
                tool_surface.allowed_write_paths,
            ),
            "project_instructions": render_project_instructions(
                self.project_instructions
            ),
            "workspace": self.runtime.redact_text(
                self.runtime.workspace.text(
                    command_runner=self.runtime.dependencies.command_runner,
                    execution_context=(
                        self.runtime.run.execution_context
                        or ExecutionContext.standalone(
                            max_seconds=WORKSPACE_GIT_TIMEOUT_SECONDS
                        )
                    ),
                )
            ),
            "user_messages": render_user_messages(
                goal,
                history.user_texts() if history is not None else (),
            ),
            "retry_instruction": render_retry_instruction(
                guidance_for_failure(projection.failure)
            ),
        }
