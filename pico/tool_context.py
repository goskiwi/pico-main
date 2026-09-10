"""Narrow context passed from runtime into tool functions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import ToolExecutionPlan
    from .execution import ExecutionContext


@dataclass
class ToolContext:
    run_id: str = "manual"
    tool_call_id: str = ""
    execution_context: ExecutionContext | None = None
    execution_plan: ToolExecutionPlan | None = None
