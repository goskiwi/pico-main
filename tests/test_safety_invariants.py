import os
import shlex
import sys
from unittest.mock import patch

from pico import cli as mini_cli
from pico import security
from pico.task_state import TaskState
from tests.helpers import build_agent


def test_workspace_escape_is_rejected(tmp_path):
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "../outside.txt"})

    assert "path escapes workspace" in result


def test_symlink_path_traversal_is_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "linked.txt"})

    assert "path escapes workspace" in result


def test_risky_tool_deny_behavior(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

    assert result == "error: shell command is not on the allowlist"
    assert agent._last_tool_result_metadata["tool_error_code"] == "shell_not_allowlisted"
    assert agent._last_tool_result_metadata["security_event_type"] == "shell_not_allowlisted"


def test_allowlisted_shell_command_reaches_approval_policy(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="never")

    result = agent.run_tool("run_shell", {"command": "pytest -q", "timeout": 20})

    assert result == "error: approval denied for run_shell"
    assert agent._last_tool_result_metadata["shell_allowlisted"] is True
    assert agent._last_tool_result_metadata["shell_allowlist_match"] == "pytest"


def test_dangerous_shell_command_is_blocked_before_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("run_shell", {"command": "rm -rf .pico", "timeout": 20})

    assert "dangerous shell command blocked" in result
    assert agent._last_tool_result_metadata["tool_status"] == "rejected"
    assert agent._last_tool_result_metadata["tool_error_code"] == "invalid_arguments"
    assert agent._last_tool_result_metadata["security_event_type"] == "dangerous_shell_command"
    assert (tmp_path / ".pico").exists()


def test_dangerous_shell_command_records_security_event_in_report(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"run_shell","args":{"command":"git reset --hard","timeout":20}}</tool>',
            "<final>Stopped safely.</final>",
        ],
        approval_policy="auto",
    )

    assert agent.ask("Try a dangerous command") == "Stopped safely."

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["summary"]["tools"] == ["run_shell"]
    assert report["summary"]["security_events"] == [
        {
            "name": "run_shell",
            "type": "dangerous_shell_command",
            "error_code": "invalid_arguments",
        }
    ]
    assert report["tool_audit"][0]["status"] == "rejected"
    assert report["tool_audit"][0]["command"] == "git reset --hard"


def test_non_allowlisted_shell_command_is_audited_when_auto_approved(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"run_shell","args":{"command":"echo hi","timeout":20}}</tool>',
            "<final>Done.</final>",
        ],
        approval_policy="auto",
    )

    assert agent.ask("Run echo") == "Done."

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    audit = report["tool_audit"][0]
    assert audit["name"] == "run_shell"
    assert audit["shell_allowlisted"] is False
    assert audit["shell_policy_reason"] == "not_allowlisted"
    assert audit["approval_decision"] == "granted"


def test_protected_write_paths_are_rejected(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("write_file", {"path": ".pico/runs/tamper.json", "content": "bad"})

    assert "protected write path blocked" in result
    assert agent._last_tool_result_metadata["tool_status"] == "rejected"
    assert agent._last_tool_result_metadata["security_event_type"] == "protected_write_path"
    assert not (tmp_path / ".pico" / "runs" / "tamper.json").exists()


def test_protected_patch_paths_are_rejected(tmp_path):
    target = tmp_path / ".env"
    target.write_text("SECRET=old\n", encoding="utf-8")
    agent = build_agent(tmp_path, [], approval_policy="auto")

    result = agent.run_tool("patch_file", {"path": ".env", "old_text": "old", "new_text": "new"})

    assert "protected write path blocked" in result
    assert agent._last_tool_result_metadata["security_event_type"] == "protected_write_path"
    assert target.read_text(encoding="utf-8") == "SECRET=old\n"


def test_structured_schema_rejects_wrong_argument_types_before_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("run_shell", {"command": ["echo", "hi"], "timeout": 20})

    assert "command must be a string" in result
    assert agent._last_tool_result_metadata["tool_status"] == "rejected"
    assert agent._last_tool_result_metadata["capability"] == "execute"
    mock_input.assert_not_called()


def test_read_only_mode_denies_non_read_capabilities(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto", read_only=True)

    result = agent.run_tool("delegate", {"role": "explore", "task": "inspect README.md", "max_steps": 2})

    assert result == "error: permission denied for delegate capability in read-only mode"
    assert agent._last_tool_result_metadata["tool_status"] == "rejected"
    assert agent._last_tool_result_metadata["tool_error_code"] == "capability_denied"
    assert agent._last_tool_result_metadata["security_event_type"] == "read_only_block"
    assert agent._last_tool_result_metadata["capability"] == "delegate"


def test_dry_run_simulates_risky_tools_without_writing(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"dry.txt","content":"hello\\n"}}</tool>',
            "<final>Dry run complete.</final>",
        ],
        approval_policy="auto",
        dry_run=True,
    )

    assert agent.ask("Create dry.txt") == "Dry run complete."
    assert not (tmp_path / "dry.txt").exists()

    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["dry_run"] is True
    assert report["summary"]["dry_run"] is True
    assert report["tool_audit"][0]["status"] == "dry_run"
    assert report["tool_audit"][0]["capability"] == "write"
    assert report["tool_audit"][0]["dry_run"] is True
    assert report["tool_audit"][0]["approval_decision"] == "dry_run"
    assert report["tool_audit"][0]["workspace_changed"] is False


def test_cli_build_agent_wires_dry_run_flag(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch("pico.cli.OllamaModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto", "--dry-run"])
        agent = mini_cli.build_agent(args)

    assert agent.dry_run is True


def test_cli_build_agent_wires_secret_env_names_from_parser(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GITHUB_PAT": "ghp-1", "GH_PAT": "ghp-2"}, clear=True), patch(
        "pico.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = mini_cli.build_arg_parser().parse_args(
            [
                "--cwd",
                str(tmp_path),
                "--approval",
                "auto",
                "--secret-env-name",
                "GITHUB_PAT",
                "--secret-env-name",
                "GH_PAT",
            ]
        )
        agent = mini_cli.build_agent(args)
        assert set(security.secret_env_summary(agent)["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}


def test_cli_build_agent_uses_default_configured_secret_names(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(os.environ, {"GH_PAT": "ghp-default-1"}, clear=True), patch(
        "pico.cli.OllamaModelClient",
        DummyModelClient,
    ):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = mini_cli.build_agent(args)
        assert security.secret_env_summary(agent)["secret_env_names"] == ["GH_PAT"]


def test_cli_build_agent_reads_secret_names_from_environment_config(tmp_path):
    class DummyModelClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def complete(self, prompt, max_new_tokens):
            raise AssertionError("model should not be invoked")

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    with patch.dict(
        os.environ,
        {
            "MCA_CUSTOM_SECRET": "custom-secret-value",
            "MINI_CODING_AGENT_SECRET_ENV_NAMES": "MCA_CUSTOM_SECRET",
        },
        clear=True,
    ), patch("pico.cli.OllamaModelClient", DummyModelClient):
        args = mini_cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--approval", "auto"])
        agent = mini_cli.build_agent(args)
        assert security.secret_env_summary(agent)["secret_env_names"] == ["MCA_CUSTOM_SECRET"]


def test_run_shell_uses_allowlisted_environment_only(tmp_path):
    secret = "shh-allowlist-secret"
    agent = build_agent(tmp_path, [], approval_policy="auto")
    script = 'import os; print(os.getenv("MCA_ALLOWLIST_SECRET", "missing"))'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with patch.dict(os.environ, {"MCA_ALLOWLIST_SECRET": secret}, clear=False):
        result = agent.run_tool("run_shell", {"command": command, "timeout": 20})

    assert secret not in result
    assert "missing" in result


def test_bound_tool_methods_delegate_into_tools_module(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="auto")

    with patch("pico.tools.subprocess.run") as fake_run:
        fake_run.return_value = type(
            "Result",
            (),
            {"returncode": 0, "stdout": "toolkit-shell\n", "stderr": ""},
        )()
        shell_result = agent.tool_run_shell({"command": "echo bypass", "timeout": 20})

    assert "toolkit-shell" in shell_result
    fake_run.assert_called_once()
    assert agent.tool_run_shell.__func__.__module__ == "pico.runtime"

    with patch("pico.tools.tool_delegate", return_value="toolkit-delegate") as fake_delegate:
        delegate_result = agent.tool_delegate({"task": "inspect README.md", "max_steps": 2})

    assert delegate_result == "toolkit-delegate"
    fake_delegate.assert_called_once()


def test_delegate_depth_limit_is_enforced(tmp_path):
    agent = build_agent(tmp_path, [], depth=1, max_depth=1)

    try:
        agent.validate_tool("delegate", {"role": "explore", "task": "inspect README.md", "max_steps": 2})
    except ValueError as exc:
        assert "delegate depth exceeded" in str(exc)
    else:
        raise AssertionError("delegate depth validation did not fail")


def test_delegate_child_is_read_only(tmp_path):
    target = tmp_path / "child-was-not-allowed.txt"
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"role":"explore","task":"write a file","max_steps":2}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"child-was-not-allowed.txt","content":"nope"}}</tool>',
            "<final>child done</final>",
            "<final>parent done</final>",
        ],
    )

    result = agent.ask("Delegate the work")

    assert result == "parent done"
    assert not target.exists()
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result role=explore" in tool_events[0]["content"]


def test_delegate_child_does_not_expose_write_tools(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"role":"review","task":"try to write a file","max_steps":2}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"child-was-not-allowed.txt","content":"nope"}}</tool>',
            "<final>child done</final>",
            "<final>parent done</final>",
        ],
    )

    assert agent.ask("Delegate the work") == "parent done"

    assert not (tmp_path / "child-was-not-allowed.txt").exists()
    child_prompt = agent.model_client.prompts[1]
    assert "- write_file(" not in child_prompt
    assert "- patch_file(" not in child_prompt
    assert "- run_shell(" not in child_prompt
    assert "- delegate(" not in child_prompt
    assert "- read_file(" in child_prompt
    assert "mode: review" in child_prompt


def test_delegate_many_children_do_not_expose_write_tools(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate_many","args":{"tasks":[{"role":"explore","task":"try to write a file","max_steps":2},{"role":"verify","task":"try to run shell","max_steps":2}]}}</tool>',
            '<tool>{"name":"write_file","args":{"path":"child-was-not-allowed.txt","content":"nope"}}</tool>',
            "<final>first child done</final>",
            '<tool>{"name":"run_shell","args":{"command":"echo nope","timeout":20}}</tool>',
            "<final>second child done</final>",
            "<final>parent done</final>",
        ],
    )

    assert agent.ask("Delegate multiple checks") == "parent done"

    assert not (tmp_path / "child-was-not-allowed.txt").exists()
    child_prompts = agent.model_client.prompts[1:3]
    assert len(child_prompts) == 2
    for child_prompt in child_prompts:
        assert "- write_file(" not in child_prompt
        assert "- patch_file(" not in child_prompt
        assert "- run_shell(" not in child_prompt
        assert "- delegate(" not in child_prompt
        assert "- delegate_many(" not in child_prompt
        assert "- read_file(" in child_prompt


def test_configured_secret_env_names_are_redacted_in_trace_and_report(tmp_path):
    github_pat = "ghp_configured_secret_123"
    gh_pat = "ghp_configured_secret_456"
    with patch.dict(os.environ, {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat}, clear=True):
        agent = build_agent(
            tmp_path,
            [],
            secret_env_names=("GITHUB_PAT", "GH_PAT"),
        )
        state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Mask configured secrets")
        agent.run_store.start_run(state)

        assert set(security.secret_env_summary(agent)["secret_env_names"]) == {"GITHUB_PAT", "GH_PAT"}

        payload = {
            "GITHUB_PAT": github_pat,
            "GH_PAT": gh_pat,
            "nested": {"GITHUB_PAT": github_pat, "GH_PAT": gh_pat},
            "list": [github_pat, gh_pat],
        }
        agent.emit_trace(state, "tool_executed", payload)
        agent.run_store.write_report(
            state,
            security.redact_artifact(agent, {"task_state": state.to_dict(), "payload": payload}),
        )

    run_dir = agent.run_store.run_dir(state.run_id)
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report_text = (run_dir / "report.json").read_text(encoding="utf-8")

    assert github_pat not in trace_text
    assert gh_pat not in trace_text
    assert github_pat not in report_text
    assert gh_pat not in report_text
    assert trace_text.count("<redacted>") >= 4
    assert report_text.count("<redacted>") >= 4
