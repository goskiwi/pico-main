"""Long-lived dependencies owned by a Pico runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .artifacts import ArtifactStore
from .command_runner import CommandRunner
from .mutations import WorkspaceMutationService
from .run_store import RunStore

if TYPE_CHECKING:
    from .contracts import ToolExecutionPlan


@dataclass(slots=True)
class RuntimeDependencies:
    run_store: RunStore
    artifacts: ArtifactStore
    mutations: WorkspaceMutationService
    command_runner: CommandRunner
    approval_handler: Callable[[str, dict, ToolExecutionPlan], bool] | None = None
