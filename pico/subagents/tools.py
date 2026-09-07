"""Parent-only tools for one synchronous Child at a time."""

from __future__ import annotations

import uuid

from pydantic import Field

from ..contracts import FailureInfo, ToolExecutionPlan, ToolRunnerResult
from .contracts import (
    ChildIntegration,
    ChildSpec,
    StrictModel,
    planned_worktree_path,
)


class DelegateArgs(ChildSpec):
    pass


class IntegrateChildArgs(StrictModel):
    child_id: str = Field(pattern=r"^child_[a-f0-9]{12}$")


def _subagent_service(context):
    service = context.subagent_service
    if service is None:
        raise RuntimeError("subagent executor is unavailable")
    return service


def _delegate_plan(context, args):
    manager = _subagent_service(context)
    return manager.plan_delegate(
        context.tool_call_id,
        args["role"],
        args["task"],
        args["allowed_write_paths"],
    )


def _delegate(context, args):
    manager = _subagent_service(context)
    receipt = manager.delegate(
        context.execution_plan,
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


def _integration_paths(context, child_id):
    manager = _subagent_service(context)
    record = manager.parent.run.projection.children.record(child_id)
    patch = record.completed().patch
    return tuple(patch.changed_paths) if patch else ()


def _integration_plan(context, args):
    manager = _subagent_service(context)
    paths = _integration_paths(context, args["child_id"])
    command = str(manager.parent.config.verification_command or "").strip()
    if not command:
        raise ValueError("Child integration requires a verification command")
    record = manager.parent.run.projection.children.record(args["child_id"])
    worktree_path = planned_worktree_path(
        manager.parent.run.projection.run_id,
        "integration_" + uuid.uuid4().hex[:12],
    )
    operation = ChildIntegration(
        record.child_id,
        record.base_sha,
        command,
        worktree_path,
    ).validate_for(record, manager.parent.run.projection.run_id)
    return ToolExecutionPlan(
        "workspace",
        tuple(
            (path, manager.parent.workspace.resolve_tool_path(path))
            for path in paths
        ),
        operation.to_dict(),
    )


def _integrate(context, args):
    manager = _subagent_service(context)
    result = manager.integrate_child(
        args["child_id"],
        context.execution_plan,
    )
    return ToolRunnerResult(
        content=f"integrated Child {result['child_id']}",
        structured=dict(result),
    )


def build_tool_registry(*, available):
    return {
        "delegate": {
            "args_schema": DelegateArgs,
            "risky": False,
            "available": bool(available),
            "description": (
                "Run one synchronous Explore or Implement Child. Explore shares the "
                "parent workspace read-only. Implement requires exact allowed write paths, "
                "a configured verifier, and an isolated Git worktree; it returns an "
                "immutable patch receipt but never integrates automatically."
            ),
            "plan": _delegate_plan,
            "run": _delegate,
        },
        "integrate_child": {
            "args_schema": IntegrateChildArgs,
            "risky": True,
            "available": bool(available),
            "workspace_mutating": True,
            "state_mutating": True,
            "plan": _integration_plan,
            "description": (
                "Explicitly verify and integrate one completed Implement Child patch into "
                "the unchanged parent repository."
            ),
            "run": _integrate,
        },
    }
