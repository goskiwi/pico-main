from pathlib import Path

from pico.sandbox import SandboxResult
from tests.helpers import UnitTestSandbox, build_agent


class _MutatingFailingSandbox(UnitTestSandbox):
    """Deterministic shell double: command changes the workspace then fails."""

    def run(self, command, *, cwd, timeout, env=None):
        del command, timeout, env
        Path(cwd, "changed-by-command.txt").write_text("partial result\n", encoding="utf-8")
        return SandboxResult(returncode=1, stdout="created partial result", stderr="command failed")


def test_shell_workspace_change_with_nonzero_exit_is_partial_success(tmp_path):
    agent = build_agent(
        tmp_path,
        [],
        approval_policy="auto",
        sandbox=_MutatingFailingSandbox(tmp_path),
    )

    result = agent.run_tool(
        "run_shell",
        {"command": "python -m compileall -q .", "timeout": 20},
    )

    metadata = agent._last_tool_result_metadata
    assert "exit_code: 1" in result
    assert (tmp_path / "changed-by-command.txt").read_text(encoding="utf-8") == "partial result\n"
    assert metadata["tool_status"] == "partial_success"
    assert metadata["tool_error_code"] == "tool_partial_success"
    assert metadata["workspace_changed"] is True
    assert metadata["affected_paths"] == ["changed-by-command.txt"]
    assert metadata["diff_summary"] == ["created:changed-by-command.txt"]
    assert metadata["shell_allowlisted"] is True
    assert metadata["sandbox_backend"] == "test"
    assert any(
        "run_shell partial_success on changed-by-command.txt" in note["text"]
        for note in agent.memory.state["process_notes"]
    )
