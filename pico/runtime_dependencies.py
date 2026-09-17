"""Long-lived dependencies owned by a Pico runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .artifacts import ArtifactStore
from .command_runner import CommandResult, CommandRunner
from .mutations import WorkspaceMutationService
from .run_store import RunStore

if TYPE_CHECKING:
    from .contracts import ToolExecutionPlan
    from .execution import ExecutionContext


class ShellRunner(Protocol):
    @property
    def execution_policy(self) -> dict[str, str]: ...

    def run(self, argv, *, cwd, timeout, env=None,
            execution_context: ExecutionContext | None = None) -> CommandResult: ...

    def reconcile(self) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class RuntimeDependencies:
    run_store: RunStore
    artifacts: ArtifactStore
    mutations: WorkspaceMutationService
    command_runner: CommandRunner
    shell_runner: ShellRunner
    approval_handler: Callable[[str, dict, ToolExecutionPlan], bool] | None = None
