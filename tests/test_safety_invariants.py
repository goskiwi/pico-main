import os
import shlex
import sys
from unittest.mock import patch

import pico.security as security
from pico.task_state import TaskState
from tests.fakes import final_action, tool_action_json
from tests.helpers import build_agent


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


def test_allowlisted_shell_command_still_requires_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "pytest -q", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_allowlisted"] is True


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
    secret = "shh-allowlist-secret"
    agent = build_agent(tmp_path, [], approval_policy="auto")
    script = 'import os; print(os.getenv("MCA_ALLOWLIST_SECRET", "missing"))'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with patch.dict(os.environ, {"MCA_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result


def test_configured_secrets_are_redacted_from_trace_and_report(tmp_path):
    github_pat = "ghp_configured_secret_123"
    with patch.dict(os.environ, {"GITHUB_PAT": github_pat}, clear=True):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT",),
        )
        state = TaskState.create(
            run_id="run_001",
            task_id="task_001",
            user_request="Mask configured secrets",
        )
        agent.run_store.start_run(state)
        payload = {
            "GITHUB_PAT": github_pat,
            "nested": {"token": github_pat},
        }
        agent.emit_trace(state, "tool_executed", payload)
        agent.run_store.write_report(
            state,
            security.redact_artifact(
                agent,
                {"task_state": state.to_dict(), "payload": payload},
            ),
        )

    run_dir = agent.run_store.run_dir(state.run_id)
    persisted_text = (
        (run_dir / "trace.jsonl").read_text(encoding="utf-8")
        + (run_dir / "report.json").read_text(encoding="utf-8")
    )
    assert github_pat not in persisted_text
    assert persisted_text.count("<redacted>") >= 4
