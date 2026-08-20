"""Long-lived services owned by a Pico runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .artifacts import ArtifactStore
from .execution import CancellationToken
from .hooks import HookRunner
from .mutations import WorkspaceMutationService
from .project_memory import ProjectMemoryStore
from .repo_map import RepoMap
from .repository_overview import RepositoryOverview
from .run_store import RunStore

if TYPE_CHECKING:
    from .subagents import SubagentManager


class SandboxService(Protocol):
    def run(self, argv, **kwargs): ...

    def identity(self) -> dict: ...


@dataclass(slots=True)
class RuntimeServices:
    run_store: RunStore
    artifacts: ArtifactStore
    project_memory: ProjectMemoryStore
    mutations: WorkspaceMutationService
    sandbox: SandboxService
    sandbox_factory: Callable[[Path], SandboxService]
    hooks: HookRunner
    repo_map: RepoMap
    repository_overview: RepositoryOverview
    subagents: SubagentManager | None = None
    parent_cancellation_token: CancellationToken | None = None
