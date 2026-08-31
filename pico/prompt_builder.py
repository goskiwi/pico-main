"""Build stable Responses instructions plus dynamic model input."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .context_manager import ContextManager
from .prompt_instructions import build_prompt_instructions
from .working_state import WorkingState

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class ModelPrompt:
    instructions: str
    input_text: str


class PromptBuilder:
    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.instructions_state = build_prompt_instructions()
        self.context = ContextManager(runtime)

    @property
    def instructions(self):
        return self.instructions_state.text

    @instructions.setter
    def instructions(self, value):
        text = str(value)
        self.instructions_state = replace(
            self.instructions_state,
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def refresh(self, *, force=False):
        """Explicitly refresh Workspace metadata outside prompt construction."""
        return self.runtime.workspace.refresh(force=force)

    def working_state_text(self):
        task_state = self.runtime.run.task
        return (
            "Task goal:\n- "
            + task_state.contract.goal
            + "\n\n"
            + task_state.working.render_panel()
            if task_state is not None
            else WorkingState().render_panel()
        )

    def prepare_compaction(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        provider_overhead_tokens=0,
        action_tools=None,
    ):
        return self.context.prepare_compaction(
            user_message,
            provider_context_tokens=provider_context_tokens,
            provider_overhead_tokens=provider_overhead_tokens,
            action_tools=action_tools,
        )

    def build(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        provider_overhead_tokens=0,
        compaction_metadata=None,
        history_override=None,
        action_tools=None,
    ):
        input_text, metadata = self.context.build(
            user_message,
            provider_context_tokens=provider_context_tokens,
            provider_overhead_tokens=provider_overhead_tokens,
            compaction_metadata=compaction_metadata,
            history_override=history_override,
            action_tools=action_tools,
        )
        metadata["prompt_cache_key"] = self.instructions_state.content_hash
        return ModelPrompt(self.instructions, input_text), metadata
