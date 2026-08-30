"""Parent-only tool surface for bounded subtask orchestration."""

from __future__ import annotations

from pydantic import Field

from ..contracts import ToolRunnerResult
from .contracts import StrictModel, SubtaskSpec
from .dag import implementation_order


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


def _apply_paths(manager, task_ids):
    records = manager._records(manager._parent_run_id())
    order = implementation_order(records, tuple(task_ids))
    return tuple(
        sorted(
            {
                path
                for task_id in order
                for path in records[task_id].changed_paths
            }
        )
    )


def _apply_effects(manager, args):
    paths = _apply_paths(manager, args["task_ids"])
    return "workspace", tuple(
        (path, manager.parent.workspace.resolve_tool_path(path)) for path in paths
    )


def _apply(manager, args):
    planned_paths = _apply_paths(manager, args["task_ids"])
    before = {
        path: manager.parent.workspace.path_state(
            manager.parent.workspace.resolve_tool_path(path)
        )
        for path in planned_paths
    }
    result = manager.integration.apply(tuple(args["task_ids"]))
    changed = tuple(result["changed_paths"])
    result["path_transitions"] = [
        {
            "path": path,
            "before_state": before[path],
            "after_state": manager.parent.workspace.path_state(
                manager.parent.workspace.resolve_tool_path(path)
            ),
            "before_artifact_id": "",
        }
        for path in changed
    ]
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
            "state_mutating": True,
            "potential_effects": lambda args: _apply_effects(manager, args),
            "description": (
                "Verify and atomically integrate completed implementation task patches into "
                "the parent workspace."
            ),
            "run": lambda args: _apply(manager, args),
        },
    }
