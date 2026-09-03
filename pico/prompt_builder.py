"""Build stable Responses instructions plus dynamic model input."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .context_manager import _ContextAssembler
from .prompt_instructions import build_prompt_instructions

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
    def __init__(self, runtime: Pico):
        self._instructions = build_prompt_instructions()
        self._repository_instructions = load_repository_instructions(
            runtime.workspace.root,
            runtime.workspace.invocation_cwd,
        )
        self._context = _ContextAssembler(runtime)

    @property
    def instructions(self):
        return self._instructions.text

    @property
    def repository_instructions(self):
        return dict(self._repository_instructions)

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
