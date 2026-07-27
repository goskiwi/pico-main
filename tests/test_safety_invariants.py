import os
from unittest.mock import patch

import pytest

from tests.fakes import final_action, tool_action_json
from tests.helpers import UnitTestSandbox, build_agent


def test_workspace_escape_and_symlink_traversal_are_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    assert "path escapes workspace" in agent.run_tool(
        "read_file",
        {"files": [{"path": "../outside.txt"}]},
    )
    assert "path escapes workspace" in agent.run_tool(
        "read_file",
        {"files": [{"path": "linked.txt"}]},
    )


def test_shell_composition_never_matches_the_allowlist(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    for command in (
        "pytest -q; rm README.md",
        "pytest -q && cat pyproject.toml",
        "python -m pytest -q $(touch injected.txt)",
        "ruff check . | sh",
    ):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})
        assert result == "error: shell command is not on the allowlist"
        assert agent._last_tool_result_metadata["shell_policy_reason"] == "shell_composition"


def test_non_allowlisted_shell_command_is_rejected_before_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool(
            "run_shell",
            {"command": "python -m pip install -e .", "timeout": 20},
        )

    assert result == "error: shell command is not on the allowlist"
    assert agent._last_tool_result_metadata["tool_error_code"] == "shell_not_allowlisted"
    assert agent._last_tool_result_metadata["approval_required"] is False
    mock_input.assert_not_called()


def test_allowlisted_shell_command_still_requires_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "pytest -q", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_allowlisted"] is True


@pytest.mark.parametrize(
    "command",
    (
        "PYTHONPATH=. pytest -q",
        "PYTHONPATH=src pytest -q",
        "PYTHONPATH=. python -m pytest -q",
        "PYTHONPATH=src python -m pytest -q",
    ),
)
def test_source_layout_pytest_commands_are_allowlisted(tmp_path, command):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_allowlisted"] is True


def test_generic_environment_wrapper_is_not_allowlisted(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool(
        "run_shell", {"command": "env PYTHONPATH=. pytest -q", "timeout": 20}
    )

    assert result == "error: shell command is not on the allowlist"
    assert agent._last_tool_result_metadata["shell_policy_reason"] == "not_allowlisted"


def test_dangerous_shell_command_is_blocked_and_audited(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            tool_action_json(
                '{"name":"run_shell","args":{"command":"git reset --hard","timeout":20}}'
            ),
            final_action("Stopped safely."),
        ],
        approval_policy="auto",
    )

    assert agent.ask("Try a dangerous command") == "Stopped safely."

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["summary"]["security_events"] == [
        {
            "name": "run_shell",
            "type": "dangerous_shell_command",
            "error_code": "invalid_arguments",
        }
    ]
    assert report["tool_audit"][0]["status"] == "rejected"


def test_protected_runtime_and_env_paths_cannot_be_written_or_read(tmp_path):
    env_path = tmp_path / "service" / ".env.test"
    env_path.parent.mkdir()
    env_path.write_text("TOKEN=secret\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    write_result = agent.run_tool(
        "write_file",
        {"path": ".pico/runs/tamper.json", "content": "bad"},
    )
    read_result = agent.run_tool("read_file", {"files": [{"path": "service/.env.test"}]})
    search_result = agent.run_tool(
        "search",
        {"pattern": "TOKEN", "path": "service/.env.test"},
    )

    assert "protected write path blocked" in write_result
    assert "protected read path blocked" in read_result
    assert "protected read path blocked" in search_result
    assert not (tmp_path / ".pico" / "runs" / "tamper.json").exists()


def test_env_example_can_be_read_without_exposing_real_env_files(tmp_path):
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=your-key\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("read_file", {"files": [{"path": ".env.example"}]})

    assert "OPENAI_API_KEY=your-key" in result


def test_structured_schema_rejects_wrong_types_before_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool(
            "run_shell",
            {"command": ["echo", "hi"], "timeout": 20},
        )

    assert "command must be a string" in result
    assert agent._last_tool_result_metadata["tool_status"] == "rejected"
    mock_input.assert_not_called()


def test_read_only_mode_denies_non_read_capabilities(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto", read_only=True)

    result = agent.run_tool(
        "delegate",
        {"role": "explore", "task": "inspect README.md", "max_steps": 2},
    )

    assert result == "error: permission denied for delegate capability in read-only mode"
    assert agent._last_tool_result_metadata["tool_error_code"] == "capability_denied"
    assert agent._last_tool_result_metadata["security_event_type"] == "read_only_block"


def test_run_shell_receives_only_the_allowlisted_environment(tmp_path):
    class InspectEnvironmentSandbox(UnitTestSandbox):
        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout
            return self._result(env or {})

        @staticmethod
        def _result(env):
            from pico.sandbox import SandboxResult

            return SandboxResult(
                returncode=0,
                stdout=env.get("MCA_ALLOWLIST_SECRET", "missing"),
            )

    secret = "shh-allowlist-secret"
    agent = build_agent(
        tmp_path,
        [],
        approval_policy="auto",
        sandbox=InspectEnvironmentSandbox(tmp_path),
    )

    with patch.dict(os.environ, {"MCA_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool(
            "run_shell",
            {"command": "python -m compileall -q pico", "timeout": 20},
        )

    assert secret not in result
    assert "missing" in result
