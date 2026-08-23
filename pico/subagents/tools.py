"""Parent-only tool surface for bounded subtask orchestration."""

from __future__ import annotations

from pydantic import Field

from ..contracts import ToolRunnerResult
from .contracts import StrictModel, SubtaskSpec


class DelegateTasksArgs(StrictModel):
    tasks: tuple[SubtaskSpec, ...] = Field(min_length=1, max_length=8)


class ApplyTaskPatchesArgs(StrictModel):
    task_ids: tuple[str, ...] = Field(min_length=1, max_length=8)


def _delegate(manager, args):
    result = manager.delegate(tuple(SubtaskSpec.model_validate(item) for item in args["tasks"]))
    return ToolRunnerResult(
        f"delegated {len(result['tasks'])} tasks",
        structured=dict(result),
    )


def _apply(manager, args):
    result = manager.integration.apply(tuple(args["task_ids"]))
    changed = tuple(result["changed_paths"])
    return ToolRunnerResult(
        content=f"applied {len(result['task_ids'])} task patches",
        structured=dict(result),
        affected_paths=changed,
        effect_scope="workspace" if changed else "none",
    )


def build_tool_registry(manager):
    return {
        "delegate_tasks": {
            "args_schema": DelegateTasksArgs,
            "risky": False,
            "description": (
                "Delegate one bounded batch of only Explore tasks or only Implement tasks "
                "to independent Pico children; mixed batches are rejected. Declare semantic "
                "dependencies explicitly. Explore tasks are read-only and return structured "
                "evidence handoffs. Implement tasks require exact allowed_write_paths and run "
                "in isolated worktrees. Every child has a hard maximum of 12 tool executions."
            ),
            "run": lambda args: _delegate(manager, args),
        },
        "apply_task_patches": {
            "args_schema": ApplyTaskPatchesArgs,
            "risky": True,
            "workspace_mutating": True,
            "description": (
                "Verify and atomically integrate completed implementation task patches into "
                "the parent workspace."
            ),
            "run": lambda args: _apply(manager, args),
        },
    }
