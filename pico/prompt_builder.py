"""Build model prompts from prefix, context, and selected project memory."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

from .context_manager import ContextManager
from .features.memory import WorkingState
from .prompt_prefix import build_prompt_prefix

if TYPE_CHECKING:
    from .runtime import Pico


class PromptBuilder:
    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.prefix_state = self._build_prefix()
        self.context = ContextManager(runtime)

    def _build_prefix(self):
        runtime = self.runtime
        return build_prompt_prefix(
            workspace=runtime.workspace.context,
            tools=runtime.tools.surface,
        )

    @property
    def prefix(self):
        return self.prefix_state.text

    @prefix.setter
    def prefix(self, value):
        text = str(value)
        self.prefix_state = replace(
            self.prefix_state,
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def refresh(self, *, force=False):
        previous_hash = self.prefix_state.content_hash
        workspace_changed = self.runtime.workspace.refresh(force=force)
        if not workspace_changed and not force:
            return False
        prefix_state = self._build_prefix()
        prefix_changed = previous_hash != prefix_state.content_hash
        if prefix_changed:
            self.prefix_state = prefix_state
        return prefix_changed

    def memory_text(self):
        task_state = self.runtime.run.task_state
        working_text = (
            task_state.working_state.render_panel()
            if task_state is not None
            else WorkingState().render_panel()
        )
        return (
            f"{working_text}\n\n"
            f"{self.runtime.dependencies.project_memory.index_text()}"
        )

    def build(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        provider_overhead_tokens=0,
    ):
        self.refresh()
        prompt, metadata = self.context.build(
            user_message,
            provider_context_tokens=provider_context_tokens,
            provider_overhead_tokens=provider_overhead_tokens,
        )
        metadata["prompt_cache_key"] = self.prefix_state.content_hash
        return prompt, metadata
