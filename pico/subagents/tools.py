"""Parent-only tools for one synchronous Child at a time."""

from __future__ import annotations

from pydantic import Field

from ..contracts import FailureInfo, ToolRunnerResult
from ..tools import history_projection
from .contracts import ChildSpec, StrictModel


class DelegateArgs(ChildSpec):
    pass


class IntegrateChildArgs(StrictModel):
    child_id: str = Field(pattern=r"^child_[a-f0-9]{12}$")


DELEGATE_HISTORY_PROJECTION = history_projection(
    arg_fields=("role", "task", "allowed_write_paths"),
    result_fields=(
        "child_id",
        "child_run_id",
        "role",
        "status",
        "error",
        "base_sha",
        "patch",
    ),
)
INTEGRATE_CHILD_HISTORY_PROJECTION = history_projection(
    arg_fields=("child_id",),
    result_fields=(
        "child_id",
        "status",
        "base_sha",
        "path_transitions",
    ),
)
HISTORY_PROJECTORS = {
    "delegate": DELEGATE_HISTORY_PROJECTION,
    "integrate_child": INTEGRATE_CHILD_HISTORY_PROJECTION,
}


def _delegate(manager, args):
    receipt = manager.delegate(
        args["role"],
        args["task"],
        args["allowed_write_paths"],
    )
    return ToolRunnerResult(
        f"Child {receipt['child_id']} finished with status {receipt['status']}",
        structured=dict(receipt),
        failure=(
            None
            if receipt["status"] == "completed"
            else FailureInfo(
                "child_failed",
                receipt["error"] or "Child did not complete",
                "retry_after_change",
            )
        ),
    )


def _integration_paths(manager, child_id):
    record = manager.parent.run.projection.children.record(child_id)
    patch = record.completed().patch
    return tuple(patch.changed_paths) if patch else ()


def _integration_effects(manager, args):
    paths = _integration_paths(manager, args["child_id"])
    return "workspace", tuple(
        (path, manager.parent.workspace.resolve_tool_path(path)) for path in paths
    )


def _integrate(manager, args):
    paths = _integration_paths(manager, args["child_id"])
    before = {
        path: manager.parent.workspace.path_state(
            manager.parent.workspace.resolve_tool_path(path)
        )
        for path in paths
    }
    result = manager.integrate_child(args["child_id"])
    after = {
        path: manager.parent.workspace.path_state(
            manager.parent.workspace.resolve_tool_path(path)
        )
        for path in paths
    }
    changed = tuple(path for path in paths if before[path] != after[path])
    result["path_transitions"] = [
        {
            "path": path,
            "before_state": before[path],
            "after_state": after[path],
            "before_artifact_id": "",
        }
        for path in changed
    ]
    return ToolRunnerResult(
        content=f"integrated Child {result['child_id']}",
        structured=dict(result),
        affected_paths=changed,
        effect_scope="workspace" if changed else "none",
    )


def build_tool_registry(manager):
    return {
        "delegate": {
            "args_schema": DelegateArgs,
            "risky": False,
            "description": (
                "Run one synchronous Explore or Implement Child. Explore shares the "
                "parent workspace read-only. Implement requires exact allowed write paths, "
                "a configured verifier, and an isolated Git worktree; it returns an "
                "immutable patch receipt but never integrates automatically."
            ),
            "run": lambda context, args: _delegate(manager, args),
            "history_projection": HISTORY_PROJECTORS["delegate"],
        },
        "integrate_child": {
            "args_schema": IntegrateChildArgs,
            "risky": True,
            "workspace_mutating": True,
            "state_mutating": True,
            "potential_effects": lambda context, args: _integration_effects(manager, args),
            "description": (
                "Explicitly verify and integrate one completed Implement Child patch into "
                "the unchanged parent repository."
            ),
            "run": lambda context, args: _integrate(manager, args),
            "history_projection": HISTORY_PROJECTORS["integrate_child"],
        },
    }
