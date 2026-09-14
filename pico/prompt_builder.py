"""Build one System Prompt and a chronological model Message sequence."""

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
from .contracts import ModelMessage
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
WORKSPACE_MAX_TOKENS = 600


def load_project_instructions(repo_root):
    """Load only the workspace-root AGENTS.md instruction file."""

    repo_root = Path(repo_root).resolve()
    path = repo_root / "AGENTS.md"
    if not path.is_file() or path.is_symlink():
        return ""
    raw = path.read_bytes()
    selected = raw[:AGENTS_MD_MAX_BYTES]
    content = selected.decode("utf-8", errors="replace")
    if len(selected) < len(raw):
        content += "\n...[AGENTS.md truncated]"
    return content


def _project_message(root, instructions):
    if not instructions:
        return None
    return ModelMessage.user(
        f"# AGENTS.md instructions for {root}\n\n"
        "<INSTRUCTIONS>\n"
        + escape(instructions, quote=False)
        + "\n</INSTRUCTIONS>"
    )


def _environment_message(workspace):
    if not workspace:
        return None
    return ModelMessage.user(
        "<environment_context>\n"
        + escape(workspace, quote=False)
        + "\n</environment_context>"
    )


def _message_text(messages):
    return json.dumps(
        [message.to_dict() for message in messages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class ModelPrompt:
    system_prompt: str
    messages: tuple[ModelMessage, ...]


class PromptBuilder:
    """Own System Prompt construction, Message projection, and Compaction."""

    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.base_system_prompt = build_prompt_instructions()
        self.project_instructions = self._load_project_instructions()
        self.tokenizer = context.Tokenizer(getattr(runtime.model_client, "model", ""))
        factory = getattr(runtime.model_client, "new_isolated_client", None)
        self.semantic_summarizer = (
            CompactionSummarizer(factory) if callable(factory) else None
        )

    def count_tokens(self, text):
        return self.tokenizer.count(text)

    def _load_project_instructions(self):
        return self.runtime.redact_text(
            load_project_instructions(self.runtime.workspace.root)
        )

    def refresh_project_instructions(self):
        current = self._load_project_instructions()
        changed = current != self.project_instructions
        self.project_instructions = current
        return changed

    def _system_prompt(self, tool_surface):
        write_scope = (
            {"mode": "none"}
            if tool_surface.mode == "ask"
            else (
                {"mode": "workspace"}
                if tool_surface.allowed_write_paths is None
                else {
                    "mode": "paths",
                    "paths": list(tool_surface.allowed_write_paths),
                }
            )
        )
        permissions = {
            "mode": tool_surface.mode,
            "tools": [tool["name"] for tool in tool_surface.action_tools],
            "write_scope": write_scope,
        }
        parts = [
            self.base_system_prompt,
            "Effective Run permissions (the Run authorization cannot expand after "
            "creation and is enforced locally):\n"
            + json.dumps(permissions, ensure_ascii=False, sort_keys=True),
        ]
        return "\n\n".join(parts)

    def _workspace_text(self):
        text = self.runtime.redact_text(
            self.runtime.workspace.text(
                command_runner=self.runtime.dependencies.command_runner,
                execution_context=(
                    self.runtime.run.execution_context
                    or ExecutionContext.standalone(
                        max_seconds=WORKSPACE_GIT_TIMEOUT_SECONDS
                    )
                ),
            )
        )
        return context.clip_complete_lines(
            text,
            WORKSPACE_MAX_TOKENS,
            token_counter=self.count_tokens,
        )

    def _messages(self, history, workspace, *, include_workspace=True):
        projection = self.runtime.run.projection
        if projection.contract is None:
            raise RuntimeError("Prompt construction requires an active TaskContract")
        messages = []
        project = _project_message(
            self.runtime.workspace.root,
            self.project_instructions,
        )
        if project is not None:
            messages.append(project)
        if include_workspace:
            environment = _environment_message(workspace)
            if environment is not None:
                messages.append(environment)
        messages.append(ModelMessage.user(projection.contract.goal))
        compacted = history.compacted_message() if history is not None else None
        if compacted is not None:
            messages.append(compacted)
        if history is not None:
            messages.extend(
                message
                for unit in history.model_message_units()
                for message in unit
            )
        correction = guidance_for_failure(projection.failure)
        if correction:
            messages.append(ModelMessage.developer(correction))
        return tuple(messages)

    def _prompt_tokens(self, system_prompt, messages):
        return self.count_tokens(system_prompt) + self.count_tokens(
            _message_text(messages)
        )

    def _input_limit(self, tool_surface):
        return (
            self.runtime.effective_context_limit_tokens
            - self.runtime.config.max_output_tokens
            - self._tool_schema_tokens(tool_surface)
        )

    def prepare(self, *, tool_surface: ResolvedToolSurface):
        history = self._history()
        workspace = self._workspace_text()
        system_prompt = self._system_prompt(tool_surface)
        messages = self._messages(history, workspace)
        return {
            "history": history,
            "workspace": workspace,
            "system_prompt": system_prompt,
            "messages": messages,
            "input_limit": self._input_limit(tool_surface),
        }

    def build_for_run(self, *, tool_surface, provider_context_tokens=None):
        prepared = self.prepare(tool_surface=tool_surface)
        plan = self.plan_compaction(
            prepared,
            provider_context_tokens=provider_context_tokens,
        )
        if plan is not None:
            self.runtime.run.run_log.append_compaction(plan)
            self.runtime.dependencies.run_store.checkpoint_if_due(
                self.runtime.run.run_log,
                force=True,
            )
        return self.build(
            prepared,
            refresh_history=plan is not None,
        )

    def build(self, prepared, *, refresh_history=False):
        history = self._history() if refresh_history else prepared["history"]
        messages = self._messages(history, prepared["workspace"])
        if self._prompt_tokens(prepared["system_prompt"], messages) > prepared[
            "input_limit"
        ]:
            messages = self._messages(
                history,
                prepared["workspace"],
                include_workspace=False,
            )
        if self._prompt_tokens(prepared["system_prompt"], messages) > prepared[
            "input_limit"
        ]:
            raise context.ContextBudgetExceeded(
                "System Prompt, project instructions, user messages, and committed "
                "conversation state exceed the model input budget"
            )
        return ModelPrompt(prepared["system_prompt"], messages)

    def plan_compaction(self, prepared, *, provider_context_tokens=None):
        run_log = self.runtime.run.run_log
        history = prepared["history"]
        if (
            run_log is None
            or history is None
            or run_log.projection.pending_tool is not None
        ):
            return None
        context_tokens = max(
            self._prompt_tokens(prepared["system_prompt"], prepared["messages"]),
            int(provider_context_tokens or 0),
        )
        if context_tokens < prepared["input_limit"]:
            return None
        if self.semantic_summarizer is None:
            raise SemanticCompactionError(
                "model client does not support isolated semantic compaction"
            )

        fixed_messages = self._messages(None, prepared["workspace"])
        fixed_tokens = self._prompt_tokens(
            prepared["system_prompt"],
            fixed_messages,
        )
        history_budget = max(0, prepared["input_limit"] - fixed_tokens)

        def build_summary(events, *, max_summary_tokens):
            try:
                return self.semantic_summarizer.summarize(
                    events,
                    task_goal=self.runtime.run.projection.contract.goal,
                    execution_context=self.runtime.run.execution_context,
                    effective_context_limit_tokens=(
                        self.runtime.effective_context_limit_tokens
                    ),
                    max_output_tokens=min(
                        SUMMARY_MAX_OUTPUT_TOKENS,
                        self.runtime.config.max_output_tokens,
                        max_summary_tokens,
                    ),
                    count_tokens=self.count_tokens,
                )
            except SemanticCompactionError:
                raise
            except (ExecutionCancelled, ExecutionDeadlineExceeded):
                raise
            except Exception as exc:
                raise SemanticCompactionError(
                    f"semantic compaction failed: {type(exc).__name__}: {exc}"
                ) from exc

        compacted = history.plan_compaction(
            retain_tokens=self.runtime.config.recent_history_tokens,
            token_counter=self.count_tokens,
            context_budget=lambda _events: (history_budget, self.count_tokens),
            context_size=lambda _events, text: fixed_tokens
            + self.count_tokens(text),
            summary_builder=build_summary,
        )
        if compacted is None:
            raise SemanticCompactionError("summary did not reduce history")
        return compacted

    def _history(self):
        run_log = self.runtime.run.run_log
        return run_log.history() if run_log is not None else None

    def _tool_schema_tokens(self, tool_surface):
        estimator = getattr(
            self.runtime.model_client,
            "estimate_action_tool_tokens",
            None,
        )
        if estimator is None:
            return 0
        return max(
            0,
            int(estimator(tool_surface.action_tools, self.count_tokens)),
        )
