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


def test_read_file_reads_multiple_files_in_one_tool_action(tmp_path):
    (tmp_path / "first.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("gamma\ndelta\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "read_file",
        {
            "files": [
                {"path": "first.txt", "start": 2, "end": 2},
                {"path": "second.txt", "start": 1, "end": 1},
            ]
        },
    )

    assert result == (
        "=== read_file metadata: first.txt; header and line numbers are not file content ===\n"
        "   2: beta\n\n"
        "=== read_file metadata: second.txt; header and line numbers are not file content ===\n"
        "   1: gamma"
    )
    assert set(agent.memory.state["file_summaries"]) == {"first.txt", "second.txt"}
    assert "beta" in agent.memory.state["file_summaries"]["first.txt"]["summary"]
    assert "gamma" in agent.memory.state["file_summaries"]["second.txt"]["summary"]
    assert agent.memory.state["process_notes"] == []


def test_read_file_rejects_legacy_single_path_arguments(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("read_file", {"path": "README.md"})

    assert result.startswith("error: invalid arguments for read_file: missing required argument: files")


def test_task_artifact_tools_expand_canvas_to_event_to_reference(tmp_path):
    agent = build_agent(tmp_path, [])
    state = agent.current_task_state = TaskState.create(
        run_id="run_current",
        task_id="task_current",
        user_request="Inspect output.",
    )
    agent.run_store.start_run(state)
    result_ref = agent.run_store.save_reference(
        state, 1, "read_file", "full tool output\nline 2\n"
    )
    agent.run_store.append_offload_event(
        state,
        node_id="N001_read_file",
        tool_name="read_file",
        args={"files": [{"path": "hello.txt"}]},
        summary="Read hello.txt",
        status="done",
        result_ref=result_ref,
    )
    agent.run_store.append_task_node(
        state,
        node_id="N001_read_file",
        summary="Read hello.txt",
        status="done",
        result_ref=result_ref,
    )

    canvas = agent.run_tool("read_task_canvas", {})
    event = agent.run_tool("read_task_event", {"node_id": "N001_read_file"})
    result = agent.run_tool("read_tool_output", {"node_id": "N001_read_file"})

    assert "N001_read_file" in canvas
    assert '"result_ref": "refs/0001_read_file.txt"' in event
    assert result == "full tool output\nline 2\n"


def test_read_tool_output_rejects_refs_outside_the_artifact_directory(tmp_path):
    agent = build_agent(tmp_path, [])
    run_dir = agent.run_store.run_dir("run_previous")
    run_dir.mkdir(parents=True)
    (run_dir / "offload.jsonl").write_text(
        '{"node_id":"N001_read_file","result_ref":"refs/../../README.md"}\n',
        encoding="utf-8",
    )

    result = agent.run_tool(
        "read_tool_output",
        {"run_id": "run_previous", "node_id": "N001_read_file"},
    )

    assert "invalid ref" in result


def test_flat_tool_schemas_are_strict_and_share_pydantic_requirements(tmp_path):
    definitions = responses_action_tools(build_agent(tmp_path, []).tools)

    assert definitions[-1]["name"] == "submit_final"
    assert all(item["type"] == "function" and item["strict"] is True for item in definitions)
    for item in definitions:
        parameters = item["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])

    read_file = next(item for item in definitions if item["name"] == "read_file")
    read_parameters = read_file["parameters"]
    assert set(read_parameters["properties"]) == {"files"}
    assert read_parameters["properties"]["files"]["maxItems"] == 5
    assert set(read_parameters["properties"]["files"]["items"]["properties"]) == {
        "path",
        "start",
        "end",
    }


def test_tool_schema_exposes_shell_and_delegate_constraints_to_the_model(tmp_path):
    agent = build_agent(tmp_path, [])
    definitions = responses_action_tools(agent.tools)
    by_name = {item["name"]: item for item in definitions}

    run_shell = by_name["run_shell"]
    assert "PYTHONPATH=src python -m pytest" in run_shell["description"]

    delegate_role = by_name["delegate"]["parameters"]["properties"]["role"]
    assert delegate_role["enum"] == ["explore", "review", "verify"]
    delegate_many_role = (
        by_name["delegate_many"]["parameters"]["properties"]["tasks"]["items"]
        ["properties"]["role"]
    )
    assert delegate_many_role["enum"] == ["explore", "review", "verify"]

    prefix = agent.build_prefix().text
    assert "PYTHONPATH=src python -m pytest" in prefix
    assert "Do not use env, pipes, redirections" in prefix
