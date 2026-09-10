"""A synchronous read-only helper with its own Session and the parent's deadline."""

from dataclasses import replace

from pydantic import Field

from .contracts import FailureInfo, ToolRunnerResult
from .session_store import SessionStore
from .tools import ToolArgs


class DelegateArgs(ToolArgs):
    task: str = Field(min_length=1, max_length=6000)
    max_turns: int = Field(default=4, ge=1, le=8)


def definition(parent):
    def run(context, args):
        from .runtime import Pico
        child = Pico.create(
            parent.model_client.client.new_isolated_client(), parent.workspace,
            session_store=SessionStore(parent.session.store.directory(parent.session.id) / "delegates"),
            config=replace(parent.config, mode="ask", allowed_write_paths=(),
                           max_agent_turns=args["max_turns"], verification_command="",
                           memory_enabled=False),
            parent_execution_context=context.execution_context, child=True,
        )
        outcome = child.ask(args["task"])
        return ToolRunnerResult(
            outcome.answer, structured={"session_id": outcome.session_id, "read_only": True,
                                        "status": outcome.status, "usage": outcome.metrics},
            failure=None if outcome.status == "completed" else
            FailureInfo("child_stopped", outcome.stop_reason, "retry_after_change"),
        )
    return {"args_schema": DelegateArgs, "risky": False,
            "description": "Delegate one bounded read-only investigation. Child cannot edit or delegate.",
            "validate": lambda _context, args: args, "run": run}
