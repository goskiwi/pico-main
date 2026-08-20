"""Prompt prefix, context projection, and task memory selection."""

from __future__ import annotations

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
        self.prefix = self.prefix_state.text
        self.context = ContextManager(runtime)
        self.last_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    def _build_prefix(self):
        runtime = self.runtime
        return build_prompt_prefix(
            workspace=runtime.workspace.context,
            tools=runtime.tools.surface,
            repository_overview=runtime.services.repository_overview,
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
            self.prefix = prefix_state.text
        self.last_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self.last_refresh)

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
                    status = "available"
                except Exception as exc:  # noqa: BLE001 - optional model boundary
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

    def build(self, user_message):
        runtime = self.runtime
        refresh = self.refresh()
        prompt, metadata = self.context.build(user_message)
        tokenizer = self.context.tokenizer
        metadata.update(
            {
                "prefix_tokens": tokenizer.count(self.prefix),
                "workspace_tokens": tokenizer.count(runtime.workspace.context.text()),
                "working_memory_tokens": tokenizer.count(
                    runtime.session.memory.render_panel()
                ),
                "memory_catalog_tokens": tokenizer.count(
                    runtime.services.project_memory.index_text()
                ),
                "request_tokens": tokenizer.count(user_message),
                "tool_count": len(runtime.tools.surface),
                "workspace_docs": len(runtime.workspace.context.project_docs),
                "recent_commits": len(runtime.workspace.context.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
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
