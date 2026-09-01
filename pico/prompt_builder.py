"""Build stable Responses instructions plus dynamic model input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .context_manager import _ContextAssembler
from .prompt_instructions import build_prompt_instructions

if TYPE_CHECKING:
    from .runtime import Pico


@dataclass(frozen=True)
class ModelPrompt:
    instructions: str
    input_text: str


class PromptBuilder:
    def __init__(self, runtime: Pico):
        self._instructions = build_prompt_instructions()
        self._context = _ContextAssembler(runtime)

    @property
    def instructions(self):
        return self._instructions.text

    def count_tokens(self, text):
        return self._context.tokenizer.count(text)

    @property
    def semantic_summarizer(self):
        return self._context.semantic_summarizer

    @semantic_summarizer.setter
    def semantic_summarizer(self, value):
        self._context.semantic_summarizer = value

    def prepare_compaction(
        self,
        user_message,
        *,
        provider_context_tokens=None,
        action_tools=None,
    ):
        return self._context.prepare_compaction(
            user_message,
            provider_context_tokens=provider_context_tokens,
            action_tools=action_tools,
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
        input_text, metadata = self._context.build(
            user_message,
            provider_context_tokens=provider_context_tokens,
            compaction_metadata=compaction_metadata,
            history_override=history_override,
            action_tools=action_tools,
        )
        return ModelPrompt(self.instructions, input_text), metadata
