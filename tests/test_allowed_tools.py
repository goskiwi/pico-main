import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)


def build_agent(tmp_path, allowed_tools=None):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        FakeModelClient([ModelAction.final("Done.")]),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        config=PicoConfig(
            approval_policy="auto",
            allowed_tools=allowed_tools,
            verification_command="",
        ),
    )


def test_allowed_tools_filter_prompt_and_execution(tmp_path):
    agent = build_agent(tmp_path, ["read_file"])
    prompt, _ = agent.prompt.build("Read")
    assert "- read_file" in prompt
    assert "- run_shell" not in prompt
    outcome = agent.tools.run("run_shell", {"command": "echo hi", "timeout": 20})
    assert outcome.status == "rejected"
    assert outcome.failure.code == "tool_not_allowed"


def test_unknown_allowed_tool_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown allowed tool"):
        build_agent(tmp_path, ["missing"])
