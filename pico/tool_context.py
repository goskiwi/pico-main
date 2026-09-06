"""Narrow context passed from runtime into tool functions."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ToolContext:
    workspace_root: Path
    path_resolver: Callable[[str], Path]
    artifact_store: object | None = None
    run_id: str = "manual"
    tool_call_id: str = ""
    working_state: object | None = None
    execution_context: object | None = None
    mutation_service: object | None = None
    command_runner: object | None = None
    check_runner: Callable | None = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))
