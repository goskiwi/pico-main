"""Tool surface, schemas, validation, approval, and execution."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from . import tools as toolkit
from .contracts import ToolCall
from .prompt_prefix import tool_signature
from .tool_context import ToolContext
from .tool_executor import ToolExecutor

if TYPE_CHECKING:
    from .runtime import Pico


class RuntimeTools:
    def __init__(self, runtime: Pico):
        self.runtime = runtime
        self.registry = self._build_registry()
        self.surface = self._apply_allowlist(self.registry)
        self.action_schemas = toolkit.build_action_tools(self.surface)
        self.executor = ToolExecutor(runtime)

    def _build_registry(self):
        tools = toolkit.build_tool_registry(self.context())
        if self.runtime.services.subagents is not None:
            from .subagents.tools import build_tool_registry

            tools.update(build_tool_registry(self.runtime.services.subagents))
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

    def signature(self):
        return tool_signature(self.surface)

    def validate(self, name, args):
        runtime = self.runtime
        if name in toolkit.BASE_TOOL_SPECS:
            validated = toolkit.validate_tool(self.context(), name, args)
        else:
            tool = self.registry.get(name)
            if tool is None:
                raise ValueError(f"unknown tool: {name}")
            validated = tool["args_schema"].model_validate(args or {}).model_dump()
        allowed_paths = runtime.config.allowed_write_paths
        if name in {"write_file", "patch_file"} and allowed_paths is not None:
            target = runtime.workspace.resolve_path(validated["path"])
            relative = target.relative_to(runtime.workspace.root).as_posix()
            if relative not in set(allowed_paths):
                raise ValueError(f"write path outside allowed scope: {relative}")
        return validated

    def context(self):
        runtime = self.runtime

        def pending_call_entry_ids():
            journal = runtime.run.journal
            if journal is None:
                return ()
            call_id = journal.pending_call_id()
            return tuple(
                entry.entry_id
                for entry in journal.entries
                if entry.kind == "assistant_tool_call" and entry.call_id == call_id
            )

        return ToolContext(
            root=runtime.workspace.root,
            path_resolver=runtime.workspace.resolve_path,
            shell_env_provider=runtime.shell_env,
            project_memory=runtime.services.project_memory,
            artifact_store=runtime.services.artifacts,
            session_id=runtime.session.data["id"],
            run_id_provider=lambda: str(
                getattr(runtime.run.task_state, "run_id", "") or "manual"
            ),
            source_entry_ids_provider=pending_call_entry_ids,
            tool_call_id_provider=lambda: (
                runtime.run.journal.pending_call_id()
                if runtime.run.journal is not None
                else ""
            ),
            mutation_service=runtime.services.mutations,
            sandbox=runtime.services.sandbox,
            execution_context_provider=lambda: (
                runtime.run.execution.child(owner="run_shell")
                if runtime.run.execution is not None
                else None
            ),
        )

    def approve(self, name, args):
        config = self.runtime.config
        if config.read_only:
            return False
        if config.approval_policy == "auto":
            return True
        if config.approval_policy == "never":
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
        self.runtime.run.last_tool_outcome = outcome
        return outcome
