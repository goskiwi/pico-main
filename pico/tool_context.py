"""Narrow context passed from runtime into tool functions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .artifacts import ArtifactStore
    from .command_runner import CommandResult, CommandRunner
    from .contracts import ToolExecutionPlan
    from .execution import ExecutionContext
    from .mutations import WorkspaceMutationService
    from .subagents.runner import SubagentRunner
    from .working_state import WorkingState


@dataclass
class ToolContext:
    workspace_root: Path
    path_resolver: Callable[[str], Path]
    artifact_store: ArtifactStore | None = None
    redact_text: Callable[[str], str] = str
    run_id: str = "manual"
    tool_call_id: str = ""
    working_state: WorkingState | None = None
    execution_context: ExecutionContext | None = None
    mutation_service: WorkspaceMutationService | None = None
    command_runner: CommandRunner | None = None
    check_runner: Callable[..., CommandResult] | None = None
    subagent_service: SubagentRunner | None = None
    execution_plan: ToolExecutionPlan | None = None

    def path(self, raw_path):
        return self.path_resolver(str(raw_path))
