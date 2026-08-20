"""Parent-only tool surface for bounded subtask orchestration."""

from __future__ import annotations

import json

from pydantic import Field

from ..contracts import ToolExecution
from .contracts import StrictModel, SubtaskSpec


class DelegateTasksArgs(StrictModel):
    tasks: tuple[SubtaskSpec, ...] = Field(min_length=1, max_length=8)


class ContinueTaskArgs(StrictModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    message: str = Field(min_length=1, max_length=6000)


class ApplyTaskPatchesArgs(StrictModel):
    task_ids: tuple[str, ...] = Field(min_length=1, max_length=8)


def _delegate(manager, args):
    result = manager.delegate(tuple(SubtaskSpec.model_validate(item) for item in args["tasks"]))
    return ToolExecution(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _continue(manager, args):
    result = manager.continue_task(args["task_id"], args["message"])
    return ToolExecution(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _apply(manager, args):
    result = manager.integration.apply(tuple(args["task_ids"]))
    changed = tuple(result["changed_paths"])
    return ToolExecution(
        content=json.dumps(result, ensure_ascii=False, sort_keys=True),
        affected_paths=changed,
        diff_summary=tuple(f"modified:{path}" for path in changed),
        effect_scope="workspace" if changed else "none",
    )


def build_tool_registry(manager):
    return {
        "delegate_tasks": {
            "args_schema": DelegateTasksArgs,
            "risky": False,
            "description": (
                "Delegate a bounded DAG of explore or implement tasks to independent Pico "
                "children. Declare semantic dependencies explicitly. Explore tasks are read-only; "
                "implement tasks require exact allowed_write_paths and run in isolated worktrees."
            ),
            "run": lambda args: _delegate(manager, args),
        },
        "continue_task": {
            "args_schema": ContinueTaskArgs,
            "risky": False,
            "description": (
                "Continue one completed child using its isolated session and workspace."
            ),
            "run": lambda args: _continue(manager, args),
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
