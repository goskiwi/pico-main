"""Day 1: follow real CLI parsing and assembly into one Pico request.

The experiment uses the production ``build_arg_parser -> build_agent -> ask``
path. Only the network model adapter is replaced by ``FakeModelClient`` so the
walkthrough is deterministic and never sends a request to a provider.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from pico import FakeModelClient, ModelAction, RunOutcome
from pico import cli as pico_cli


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def component_snapshot(agent):
    """Show the eight public objects assembled around one Pico runtime."""

    return {
        "1_model_client": type(agent.model_client).__name__,
        "2_config": type(agent.config).__name__,
        "3_workspace": type(agent.workspace).__name__,
        "4_run": type(agent.run).__name__,
        "5_session": type(agent.session).__name__,
        "6_dependencies": type(agent.dependencies).__name__,
        "7_tools": type(agent.tools).__name__,
        "8_prompt": type(agent.prompt).__name__,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day1-") as directory:
        root = Path(directory)
        (root / "README.md").write_text(
            "# Demo project\n\nThis README is read by Pico's file tool.\n",
            encoding="utf-8",
        )
        argv = [
            "--mode",
            "ask",
            "--cwd",
            str(root),
            "请读取 README.md，然后告诉我是否读取成功。",
        ]
        args = pico_cli.build_arg_parser().parse_args(argv)
        user_message = " ".join(args.prompt).strip()

        fake_model = FakeModelClient(
            [
                ModelAction.tool(
                    "read_file",
                    {"path": "README.md", "start_line": 1, "end_line": 20},
                    call_id="call_readme",
                ),
                ModelAction.final("README 已读取，文件工具工作正常。"),
            ]
        )
        with patch("pico.cli._build_model_client", return_value=fake_model):
            agent = pico_cli.build_agent(args)

        assert agent.model_client is fake_model
        print_section(
            "1. CLI 只接收自然语言任务",
            {
                "argv": argv,
                "prompt": user_message,
                "user_declared_contract_fields": False,
                "provider_note": (
                    "FakeModelClient 只替代网络 Provider；CLI 解析、build_agent "
                    "和 Runtime 都使用真实代码"
                ),
            },
        )
        print_section("2. build_agent 组装出的八个顶层组件", component_snapshot(agent))
        print_section(
            "3. 启动时的恢复探测（这是一个全新 Session）",
            {
                "active_run_id": agent.session.data["active_run_id"],
                "projection_run_id": agent.run.projection.run_id,
                "resumable": agent.run.resumable,
                "reload_required": agent.run.reload_required,
            },
        )

        outcome = agent.ask(user_message)
        assert isinstance(outcome, RunOutcome)
        assert outcome.status == "completed"
        assert outcome.answer == "README 已读取，文件工具工作正常。"
        assert agent.session.data["active_run_id"] == ""
        assert agent.run.task.contract.allows_workspace_mutation is False

        events = agent.run.run_log.events
        event_rows = [
            {"sequence": event.sequence, "kind": event.kind}
            for event in events
        ]
        assert outcome.run_id == agent.run.projection.run_id
        assert not any(event.kind == "run_outcome" for event in events)

        print_section(
            "4. ask 返回完整 RunOutcome",
            {"run_outcome": outcome.to_dict()},
        )
        print_section(
            "5. 终态 Session 与 Run Log",
            {
                "session_active_run_id": agent.session.data["active_run_id"],
                "events": event_rows,
                "run_outcome_is_persisted_fact": False,
                "explanation": (
                    "RunOutcome 是从终态 RunProjection 截取的返回值；"
                    "持久化事实仍然是 Run Log events"
                ),
            },
        )


if __name__ == "__main__":
    main()
