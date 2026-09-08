"""Narrow context passed from runtime into tool functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import ToolExecutionPlan
    from .execution import ExecutionContext
    from .working_state import WorkingState


@dataclass
class ToolContext:
    run_id: str = "manual"
    tool_call_id: str = ""
    working_state: WorkingState | None = None
    execution_context: ExecutionContext | None = None
    execution_plan: ToolExecutionPlan | None = None
