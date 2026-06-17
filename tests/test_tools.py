import subprocess
from unittest.mock import patch

from tests.helpers import build_agent


def test_agent_accepts_xml_write_file_tool(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
    )

    answer = agent.ask("Create hello.py")

    assert answer == "Done."
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == 'print("hi")\n'


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "world",
            "new_text": "agent",
        },
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_does_not_prompt_for_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith("error: invalid arguments for write_file: missing required argument: path")
    assert 'example: <tool name="write_file"' in result
    mock_input.assert_not_called()


def test_list_files_hides_internal_agent_state(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / ".pico").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")

    result = agent.run_tool("list_files", {})

    assert ".pico" not in result
    assert ".git" not in result
    assert "[F] hello.txt" in result


def test_search_reports_rg_errors_explicitly(tmp_path):
    agent = build_agent(tmp_path, [])

    completed = subprocess.CompletedProcess(
        args=["rg"],
        returncode=2,
        stdout="",
        stderr="regex parse error",
    )
    with patch("pico.tools.shutil.which", return_value="/usr/bin/rg"), patch("pico.tools.subprocess.run", return_value=completed):
        result = agent.run_tool("search", {"pattern": "[", "path": "."})

    assert result == "error: search failed: regex parse error"


def test_repeated_identical_tool_call_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "1"})
    agent.record({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "2"})

    result = agent.run_tool("list_files", {})

    assert result == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"


def test_delegate_requires_explicit_role(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("delegate", {"task": "inspect README.md", "max_steps": 2})

    assert result.startswith("error: invalid arguments for delegate: missing required argument: role")
    assert '"role":"explore"' in result


def test_delegate_rejects_unknown_role(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("delegate", {"role": "builder", "task": "inspect README.md", "max_steps": 2})

    assert "unsupported delegate role: builder" in result


def test_delegate_many_requires_non_empty_tasks(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("delegate_many", {"tasks": []})

    assert result.startswith("error: invalid arguments for delegate_many: tasks must contain at least 1 item")
    assert "delegate_many" in result


def test_delegate_many_validates_each_task(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("delegate_many", {"tasks": [{"role": "explore", "task": ""}]})

    assert "tasks[1].task must not be empty" in result


def test_delegate_many_rejects_unknown_role(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("delegate_many", {"tasks": [{"role": "builder", "task": "inspect README.md"}]})

    assert "unsupported delegate role: builder" in result
