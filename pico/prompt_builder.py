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
        prefix_state = (
            self._build_prefix() if workspace_changed or force else self.prefix_state
        )
        prefix_changed = force or previous_hash != prefix_state.content_hash
        if prefix_changed:
            self.prefix_state = prefix_state
        return {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }

    def memory_text(self):
        task_state = self.runtime.run.task_state
        working_text = (
            task_state.working_state.render_panel()
            if task_state is not None
            else WorkingState().render_panel()
        )
        return (
            f"{working_text}\n\n"
            f"{self.runtime.services.project_memory.index_text()}"
        )

    def build(self, user_message, *, provider_context_tokens=None):
        runtime = self.runtime
        refresh = self.refresh()
        prompt, metadata = self.context.build(
            user_message,
            provider_context_tokens=provider_context_tokens,
        )
        metadata.update(
            {
                "prefix_hash": self.prefix_state.content_hash,
                "prompt_cache_key": self.prefix_state.content_hash,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(
                    getattr(runtime.model_client, "supports_prompt_cache", False)
                ),
                "resume_status": runtime.recovery.state.get(
                    "status", "no-active-run"
                ),
            }
        )
        metadata.update(runtime.detected_secret_env_summary())
        return prompt, metadata
