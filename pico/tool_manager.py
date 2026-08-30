"""Build, validate, approve, and execute the agent's tool set."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from . import tools as toolkit
from .contracts import ToolCall, ToolFailureError
from .tool_context import ToolContext
from .tool_executor import ToolExecutor

if TYPE_CHECKING:
    from .runtime import Pico


def intersect_write_scopes(contract_paths, policy_paths):
    if contract_paths is None:
        return policy_paths
    if policy_paths is None:
        return contract_paths
    policy = set(policy_paths)
    return tuple(path for path in contract_paths if path in policy)


class ToolManager:
    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.registry = self._build_registry()
        self.surface = self._apply_allowlist(self.registry)
        self.action_schemas = toolkit.build_action_tools(self.surface)
        self.executor = ToolExecutor(runtime)

    def _build_registry(self):
        tools = toolkit.build_tool_registry(self.context())
        if self.runtime.dependencies.subagents is not None:
            from .subagents.tools import build_tool_registry

            tools.update(build_tool_registry(self.runtime.dependencies.subagents))
        return tools

    def _apply_allowlist(self, tools):
        allowed_tools = self.runtime.config.allowed_tools
        if allowed_tools is None:
            return tools
        unknown = [name for name in allowed_tools if name not in tools]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(allowed_tools)
        return {name: tool for name, tool in tools.items() if name in allowed}

    def validate(self, name, args):
        runtime = self.runtime
        task = runtime.run.task
        tool = self.registry.get(name)
        contract = getattr(task, "contract", None)
        if (
            contract is not None
            and contract.task_kind == "read_only"
            and tool is not None
            and tool.get("state_mutating", False)
        ):
            raise ToolFailureError(
                "read_only_task",
                f"task requirements do not allow state-mutating tool: {name}",
                "no_retry",
            )
        if tool is None:
            raise ValueError(f"unknown tool: {name}")
        validated = tool["args_schema"].model_validate(args or {}).model_dump()
        validator = tool.get("validate")
        if validator is not None:
            validated = validator(validated)
        if (
            contract is not None
            and contract.task_kind == "read_only"
            and name == "delegate_tasks"
            and any(item.get("kind") == "implement" for item in validated["tasks"])
        ):
            raise ToolFailureError(
                "read_only_task",
                "read-only task may delegate explore tasks only",
                "no_retry",
            )
        allowed_paths = intersect_write_scopes(
            getattr(contract, "allowed_write_paths", None),
            runtime.config.allowed_write_paths,
        )
        if name in {"write_file", "edit_file"} and allowed_paths is not None:
            target = runtime.workspace.resolve_tool_path(validated["path"])
            relative = target.relative_to(runtime.workspace.root).as_posix()
            if relative not in set(allowed_paths):
                raise ValueError(f"write path outside allowed scope: {relative}")
        return validated

    def model_action_tools(self):
        task = self.runtime.run.task
        contract = getattr(task, "contract", None)
        if contract is None or contract.task_kind != "read_only":
            return list(self.action_schemas)
        return [
            schema
            for schema in self.action_schemas
            if schema["name"] == "submit_final"
            or not self.registry.get(schema["name"], {}).get(
                "state_mutating", False
            )
        ]

    def context(self):
        runtime = self.runtime

        return ToolContext(
            workspace_root=runtime.workspace.root,
            path_resolver=runtime.workspace.resolve_tool_path,
            shell_env_provider=runtime.shell_env,
            project_memory=runtime.dependencies.project_memory,
            artifact_store=runtime.dependencies.artifacts,
            run_id_provider=lambda: str(
                runtime.run.projection.run_id or "manual"
            ),
            tool_call_id_provider=lambda: (
                runtime.run.run_log.pending_call_id()
                if runtime.run.run_log is not None
                else ""
            ),
            working_state_provider=lambda: (
                runtime.run.task.working
                if runtime.run.task is not None
                else None
            ),
            token_counter_provider=lambda text: runtime.prompt.context.tokenizer.count(
                text
            ),
            mutation_service=runtime.dependencies.mutations,
            sandbox=runtime.dependencies.sandbox,
            execution_context_provider=lambda: (
                runtime.run.execution_context.child()
                if runtime.run.execution_context is not None
                else None
            ),
        )

    def approve(self, name, args):
        config = self.runtime.config
        if config.approval_policy == "auto":
            return True
        if config.approval_policy == "deny":
            return False
        try:
            answer = input(
                f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] "
            )
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def run(self, call_or_name, args=None):
        call = (
            call_or_name
            if isinstance(call_or_name, ToolCall)
            else ToolCall(str(call_or_name), dict(args or {}))
        )
        outcome = self.executor.execute(call)
        return outcome
