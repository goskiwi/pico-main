from unittest.mock import patch

from pico.task_state import TaskState
from pico.tools import responses_action_tools
from tests.helpers import build_agent


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {"path": "sample.txt", "old_text": "world", "new_text": "agent"},
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_is_rejected_before_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith(
        "error: invalid arguments for write_file: missing required argument: path"
    )
    mock_input.assert_not_called()


def test_local_validation_rejects_provider_bypassed_extra_arguments(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "list_files",
        {"path": ".", "unexpected": "value"},
    )

    assert result.startswith("error: invalid arguments for list_files: unexpected argument")


def test_read_tool_output_resolves_the_task_graph_reference(tmp_path):
    agent = build_agent(tmp_path, [])
    state = agent.current_task_state = TaskState.create(
        run_id="run_current",
        task_id="task_current",
        user_request="Inspect output.",
    )
    run_dir = agent.run_store.start_run(state)
    output_path = run_dir / "tool_outputs" / "0001_read_file.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("full tool output\nline 2\n", encoding="utf-8")
    (run_dir / "task_graph.mmd").write_text(
        "flowchart TD\n"
        '  t001_read_file["tool | ok | read_file hello.txt"]\n'
        "  %% t001_read_file ref: tool_outputs/0001_read_file.txt\n",
        encoding="utf-8",
    )

    result = agent.run_tool("read_tool_output", {"node_id": "t001_read_file"})

    assert result == "full tool output\nline 2\n"


def test_read_tool_output_rejects_refs_outside_the_artifact_directory(tmp_path):
    agent = build_agent(tmp_path, [])
    run_dir = agent.run_store.run_dir("run_previous")
    run_dir.mkdir(parents=True)
    (run_dir / "task_graph.mmd").write_text(
        "flowchart TD\n"
        '  t001_read_file["tool | ok | bad ref"]\n'
        "  %% t001_read_file ref: tool_outputs/../../README.md\n",
        encoding="utf-8",
    )

    result = agent.run_tool(
        "read_tool_output",
        {"run_id": "run_previous", "node_id": "t001_read_file"},
    )

    assert "invalid ref" in result


def test_responses_tool_schemas_are_strict_and_share_pydantic_requirements(tmp_path):
    definitions = responses_action_tools(build_agent(tmp_path, []).tools)

    assert definitions[-1]["name"] == "submit_final"
    assert all(item["type"] == "function" and item["strict"] is True for item in definitions)
    for item in definitions:
        parameters = item["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])
