"""Prompt prefix, context projection, and task memory selection."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

from .context_manager import ContextManager
from .project_memory import MEMORY_SELECTOR_MAX_SELECTED
from .prompt_prefix import build_prompt_prefix
from .workspace import clip

if TYPE_CHECKING:
    from .runtime import Pico


class RuntimePrompt:
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
            hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def refresh(self, *, force=False):
        previous_hash = self.prefix_state.hash
        workspace_changed = self.runtime.workspace.refresh(force=force)
        prefix_state = (
            self._build_prefix() if workspace_changed or force else self.prefix_state
        )
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self.prefix_state = prefix_state
        return {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }

    def memory_text(self):
        return (
            f"{self.runtime.session.memory.render_panel()}\n\n"
            f"{self.runtime.services.project_memory.index_text()}"
        )

    def select_memory(self, user_message, *, budget_tokens, token_counter):
        runtime = self.runtime
        if runtime.run.task_memory_selection is None:
            manifest = runtime.services.project_memory.selector_manifest()
            selector = getattr(runtime.model_client, "select_memory_filenames", None)
            filenames = []
            status = "empty" if not manifest else "unavailable"
            failure = {}
            if manifest and callable(selector):
                try:
                    filenames = selector(
                        user_message,
                        manifest,
                        max_files=MEMORY_SELECTOR_MAX_SELECTED,
                        max_new_tokens=192,
                    )
                    if (
                        not isinstance(filenames, list)
                        or len(filenames) > MEMORY_SELECTOR_MAX_SELECTED
                        or any(
                            not isinstance(filename, str) or not filename
                            for filename in filenames
                        )
                        or len(set(filenames)) != len(filenames)
                    ):
                        raise ValueError("memory selector returned invalid filenames")
                    allowed = {str(item["filename"]) for item in manifest}
                    if any(filename not in allowed for filename in filenames):
                        raise ValueError(
                            "memory selector returned an unavailable filename"
                        )
                    status = "available"
                except Exception as exc:  # noqa: BLE001 - optional model boundary
                    filenames = []
                    failure = {
                        "code": "memory_selector_failed",
                        "detail": clip(str(exc), 300),
                    }
            runtime.run.task_memory_selection = {
                "query": str(user_message),
                "cards": runtime.services.project_memory.selected_cards(filenames),
                "status": status,
                "failure": failure,
                "available_count": len(manifest),
            }
        selected = runtime.run.task_memory_selection
        project_text, included_cards = (
            runtime.services.project_memory.render_selected_with_budget(
                selected["cards"],
                max_tokens=budget_tokens,
                token_counter=token_counter,
            )
        )
        return (
            project_text,
            {
                "status": selected["status"],
                "failure": dict(selected["failure"]),
                "available_count": selected["available_count"],
                "candidate_filenames": [card.filename for card in selected["cards"]],
                "selected_filenames": [card.filename for card in included_cards],
            },
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
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
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
