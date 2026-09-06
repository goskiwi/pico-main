"""Long-lived dependencies owned by a Pico runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import ArtifactStore
from .command_runner import CommandRunner
from .execution import ExecutionContext
from .mutations import WorkspaceMutationService
from .repo_map import RepoMap
from .run_store import RunStore

if TYPE_CHECKING:
    from .subagents.runner import SubagentRunner


@dataclass(slots=True)
class RuntimeDependencies:
    run_store: RunStore
    artifacts: ArtifactStore
    mutations: WorkspaceMutationService
    command_runner: CommandRunner
    command_runner_factory: Callable[[Path], CommandRunner]
    repo_map: RepoMap
    subagents: SubagentRunner | None = None
    parent_execution_context: ExecutionContext | None = None
    check_runner: Callable | None = None
