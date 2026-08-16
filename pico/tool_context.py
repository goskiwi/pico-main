"""Narrow context passed from runtime into tool functions."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolContext:
    root: Path
    path_resolver: Callable[[str], Path]
    shell_env_provider: Callable[[], dict]
    project_memory: object | None = None
    session_id: str = ""
    run_id_provider: Callable[[], str] | None = None
    source_entry_ids_provider: Callable[[], tuple[str, ...]] | None = None
    tool_call_id_provider: Callable[[], str] | None = None
    repo_map: object | None = None
    mutation_service: object | None = None
    sandbox: object | None = None
    execution_context_provider: Callable[[], object] | None = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def shell_env(self):
        return self.shell_env_provider()

    def run_id(self):
        return self.run_id_provider() if self.run_id_provider else ""

    def source_entry_ids(self):
        return self.source_entry_ids_provider() if self.source_entry_ids_provider else ()

    def tool_call_id(self):
        return self.tool_call_id_provider() if self.tool_call_id_provider else ""

    def execution_context(self):
        return self.execution_context_provider() if self.execution_context_provider else None
