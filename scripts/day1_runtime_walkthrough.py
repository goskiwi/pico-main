"""Day 1: inspect Pico's nine top-level components before and after ask()."""

import json
import tempfile
from pathlib import Path

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)


def component_snapshot(agent):
    """Return a small, beginner-friendly view of Pico's nine components."""
    task = agent.run.task_state
    run_log = agent.run.run_log
    return {
        "1_model_client": {
            "type": type(agent.model_client).__name__,
            "queued_actions": len(agent.model_client.outputs),
            "prompts_received": len(agent.model_client.prompts),
            "tool_results_received": len(agent.model_client.recorded_action_results),
        },
        "2_config": {
            "approval_policy": agent.config.approval_policy,
            "verification_command": agent.config.verification_command,
            "max_new_tokens": agent.config.max_new_tokens,
        },
        "3_workspace": {
            "root": str(agent.workspace.root),
            "revision": agent.workspace.revision,
        },
        "4_session": {
            "id": agent.session.data["id"],
            "active_run_id": agent.session.data["active_run_id"],
        },
        "5_run": {
            "has_task_state": task is not None,
            "has_run_log": run_log is not None,
            "status": task.status if task is not None else None,
            "model_requests": task.model_request_count if task is not None else 0,
            "executed_tools": task.executed_tool_count if task is not None else 0,
        },
        "6_dependencies": {
            "run_store": type(agent.dependencies.run_store).__name__,
            "artifact_store": type(agent.dependencies.artifacts).__name__,
            "project_memory": type(agent.dependencies.project_memory).__name__,
            "mutation_service": type(agent.dependencies.mutations).__name__,
            "sandbox": type(agent.dependencies.sandbox).__name__,
            "repo_map": type(agent.dependencies.repo_map).__name__,
        },
        "7_tools": {
            "type": type(agent.tools).__name__,
            "visible_tools": sorted(agent.tools.surface),
        },
        "8_recovery": dict(agent.recovery.state),
        "9_prompt": {
            "type": type(agent.prompt).__name__,
            "prefix_characters": len(agent.prompt.prefix),
        },
    }


def event_snapshot(agent):
    """Show the durable event sequence produced by this demonstration."""
    return [
        {
            "sequence": event.sequence,
            "kind": event.kind,
            "tool": event.name,
            "status": event.outcome_status,
        }
        for event in agent.run.run_log.events
    ]


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day1-") as directory:
        root = Path(directory)
        (root / "README.md").write_text(
            "# Demo project\n\nThis README is read by Pico's file tool.\n",
            encoding="utf-8",
        )

        fake_model = FakeModelClient(
            [
                ModelAction.tool(
                    "read_file",
                    {"path": "README.md", "start_line": 1, "end_line": 20},
                ),
                ModelAction.final("README 已读取，文件工具工作正常。"),
            ]
        )
        agent = Pico(
            model_client=fake_model,
            workspace=WorkspaceContext.build(root),
            session_store=SessionStore(root / ".pico" / "sessions"),
            config=PicoConfig(
                approval_policy="auto",
                verification_command="",
            ),
        )

        print_section("ask() 之前", component_snapshot(agent))

        answer = agent.ask("请读取 README.md，然后告诉我是否读取成功。")

        print_section("最终回答", answer)
        print_section("ask() 之后", component_snapshot(agent))
        print_section("Run Log 事件", event_snapshot(agent))
        print_section(
            "FakeModelClient 收到的工具结果",
            [result for _kind, result in fake_model.recorded_action_results],
        )


if __name__ == "__main__":
    main()
