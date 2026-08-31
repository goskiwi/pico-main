"""Long-lived dependencies owned by a Pico runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import ArtifactStore
from .command_runner import CommandRunner
from .execution import CancellationToken
from .mutations import WorkspaceMutationService
from .repo_map import RepoMap
from .run_store import RunStore
from .task_classifier import TaskClassifier

if TYPE_CHECKING:
    from .subagents import SubagentRunner


@dataclass(slots=True)
class RuntimeDependencies:
    run_store: RunStore
    artifacts: ArtifactStore
    mutations: WorkspaceMutationService
    command_runner: CommandRunner
    command_runner_factory: Callable[[Path], CommandRunner]
    repo_map: RepoMap
    task_classifier: TaskClassifier
    subagents: SubagentRunner | None = None
    parent_cancellation_token: CancellationToken | None = None
