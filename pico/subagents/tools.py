"""Parent-only tools for one synchronous Child at a time."""

from __future__ import annotations

import uuid
from functools import partial

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


def _subagent_service(service):
    if service is None:
        raise RuntimeError("subagent executor is unavailable")
    return service


def _delegate_plan(context, args, *, service):
    manager = _subagent_service(service)
    return manager.plan_delegate(
        context.tool_call_id,
        args["role"],
        args["task"],
        args["allowed_write_paths"],
    )


def _delegate(context, args, *, service):
    manager = _subagent_service(service)
    receipt = manager.delegate(
        context.execution_plan,
        args["role"],
        args["task"],
        args["allowed_write_paths"],
    )
    error = receipt.pop("error", None)
    return ToolRunnerResult(
        "",
        structured=dict(receipt),
        failure=(
            None
            if receipt["status"] == "completed"
            else FailureInfo(
                "child_failed",
                error or "Child did not complete",
                "retry_after_change",
            )
        ),
    )


def _integration_paths(service, child_id):
    manager = _subagent_service(service)
    record = manager.parent.run.projection.children.record(child_id)
    patch = record.completed().patch
    return tuple(patch.changed_paths) if patch else ()


def _integration_plan(context, args, *, service):
    manager = _subagent_service(service)
    paths = _integration_paths(service, args["child_id"])
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


def _integrate(context, args, *, service):
    manager = _subagent_service(service)
    result = manager.integrate_child(
        args["child_id"],
        context.execution_plan,
    )
    return ToolRunnerResult(
        content=f"integrated Child {result['child_id']}",
        structured=dict(result),
    )


def build_tool_registry(*, service):
    return {
        "delegate": {
            "args_schema": DelegateArgs,
            "risky": False,
            "available": service is not None,
            "description": (
                "Run one synchronous Explore or Implement Child. Explore shares the "
                "parent workspace read-only. Implement requires exact allowed write paths, "
                "a configured verifier, and an isolated Git worktree; it returns an "
                "immutable patch receipt but never integrates automatically."
            ),
            "plan": partial(_delegate_plan, service=service),
            "run": partial(_delegate, service=service),
        },
        "integrate_child": {
            "args_schema": IntegrateChildArgs,
            "risky": True,
            "available": service is not None,
            "workspace_mutating": True,
            "state_mutating": True,
            "plan": partial(_integration_plan, service=service),
            "description": (
                "Explicitly verify and integrate one completed Implement Child patch into "
                "the unchanged parent repository."
            ),
            "run": partial(_integrate, service=service),
        },
    }
