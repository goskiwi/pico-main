"""Narrow context passed from runtime into tool functions."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolContext:
    workspace_root: Path
    path_resolver: Callable[[str], Path]
    artifact_store: object | None = None
    run_id_provider: Callable[[], str] | None = None
    tool_call_id_provider: Callable[[], str] | None = None
    working_state_provider: Callable[[], object | None] | None = None
    execution_context_provider: Callable[[], object | None] | None = None
    mutation_service: object | None = None
    command_runner: object | None = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))

    def run_id(self):
        return self.run_id_provider() if self.run_id_provider else ""

    def tool_call_id(self):
        return self.tool_call_id_provider() if self.tool_call_id_provider else ""

    def working_state(self):
        return self.working_state_provider() if self.working_state_provider else None

    def execution_context(self):
        return self.execution_context_provider() if self.execution_context_provider else None
