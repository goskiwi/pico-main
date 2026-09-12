"""Build stable Responses instructions plus dynamic model input."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
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
CONTEXT_WIRE_ORDER = ("runtime_evidence", "workspace", "history")


def load_repository_instructions(repo_root):
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


def _untrusted_envelope(selected):
    lines = ['<untrusted_context trust="untrusted_data">']
    for section in CONTEXT_WIRE_ORDER:
        value = str(selected.get(section, "")).strip()
        if value:
            lines.extend(
                (
                    f'<section name="{section}">',
                    escape(value, quote=False),
                    "</section>",
                )
            )
    lines.append("</untrusted_context>")
    return "\n".join(lines)


def _assemble_input(raw, selected):
    parts = [raw["runtime_policy"]]
    if raw["repository_instructions"]:
        parts.append(raw["repository_instructions"])
    parts.append(raw["task_request"])
    if raw["runtime_instruction"]:
        parts.append(raw["runtime_instruction"])
    if selected:
        parts.append(_untrusted_envelope(selected))
    if raw["latest_user_request"]:
        parts.append(raw["latest_user_request"])
    return "\n\n".join(parts)


def runtime_feedback_sections(feedback):
    if feedback is None:
        return {"runtime_instruction": "", "runtime_evidence": ""}
    return {
        "runtime_instruction": "runtime_instruction:\n"
        + json.dumps(
            {"instruction": feedback.instruction},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "runtime_evidence": (
            "runtime_evidence:\n"
            + json.dumps(
                {
                    "content": feedback.evidence,
                    "artifact_id": feedback.evidence_artifact_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if feedback.evidence or feedback.evidence_artifact_id
            else ""
        ),
    }


def render_runtime_feedback(feedback):
    sections = runtime_feedback_sections(feedback)
    parts = [sections["runtime_instruction"]]
    if sections["runtime_evidence"]:
        parts.append(
            _untrusted_envelope(
                {"runtime_evidence": sections["runtime_evidence"]}
            )
        )
    return "\n\n".join(parts)


def render_runtime_policy(contract, mode, paths, verification_required):
    if contract is None:
        policy = {
            "mode": "unavailable",
            "verification_required": False,
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
            "verification_required": bool(verification_required),
            "write_scope": write_scope,
        }
    return "runtime_policy:\n" + json.dumps(
        policy, ensure_ascii=False, sort_keys=True
    )


def render_repository_instructions(instructions):
    if not instructions:
        return ""
    content = instructions["AGENTS.md"]
    return "\n".join(
        (
            "<repository_instructions>",
            '<instructions path="AGENTS.md">',
            "Applies to the entire workspace.",
            escape(content, quote=False),
            "</instructions>",
            "</repository_instructions>",
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
        self.repository_instructions = load_repository_instructions(runtime.workspace.root)
        self.tokenizer = context.Tokenizer(getattr(runtime.model_client, "model", ""))
        self.section_caps = dict(context.DEFAULT_SECTION_CAPS)
        factory = getattr(runtime.model_client, "new_isolated_client", None)
        self.semantic_summarizer = (
            CompactionSummarizer(factory) if callable(factory) else None
        )

    def count_tokens(self, text):
        return self.tokenizer.count(text)

    def refresh_repository_instructions(self):
        current = load_repository_instructions(self.runtime.workspace.root)
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
        fixed = context.fixed_context(
            raw,
            section_caps=self.section_caps,
            count_tokens=self.count_tokens,
            render_input=_assemble_input,
        )
        return {
            "raw": raw,
            "instructions_tokens": instructions_tokens,
            "tool_schema_tokens": tool_schema_tokens,
            "available": available,
            "fixed_context": fixed,
            "history_budget": context.history_budget(
                raw,
                available,
                fixed_context=fixed,
                count_tokens=self.count_tokens,
                render_input=_assemble_input,
            ),
            "history_token_counter": context.history_token_counter(
                raw,
                fixed,
                count_tokens=self.count_tokens,
                render_input=_assemble_input,
            ),
        }

    def build(self, inputs):
        """Render one prompt after Context performs the sole History selection."""
        raw = inputs["raw"]
        history = self._history()
        count_tokens = self.count_tokens
        available = inputs["available"]
        history_text = render_history(history)
        raw = {**raw, "history": history_text}
        minimum_input = _assemble_input(raw, context._required_context(raw))
        if count_tokens(minimum_input) > available:
            raise context.ContextBudgetExceeded(
                "runtime policy, repository instructions, task request, pending "
                "Runtime instruction and evidence exceed the model budget"
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
        if run_log is None or run_log.pending_tool_call() is not None:
            return None
        history = self._history()
        config = self.runtime.config
        count_tokens = self.count_tokens
        history_text = render_history(history)
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
        reserve_tokens = max(
            int(config.max_new_tokens), config.compaction_reserve_tokens
        )
        threshold_tokens = max(1, config.provider_context_limit_tokens - reserve_tokens)
        if context_tokens < threshold_tokens:
            return None
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
            return None
        summary, covered = compacted
        return summary, covered

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
            "runtime_policy": render_runtime_policy(
                contract,
                tool_surface.mode,
                tool_surface.allowed_write_paths,
                bool(
                    verification_policy
                    and verification_policy.required
                ),
            ),
            "repository_instructions": render_repository_instructions(
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
            **runtime_feedback_sections(feedback),
            "latest_user_request": (
                "latest_user_request:\n" + json.dumps(latest, ensure_ascii=False)
                if latest
                else ""
            ),
        }
