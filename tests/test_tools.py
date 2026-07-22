import subprocess
from unittest.mock import patch

from pico.task_state import TaskState
from pico.tools import responses_action_tools
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


def test_read_file_marks_display_metadata_as_not_file_content(tmp_path):
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "read_file", {"path": "sample.txt", "start": 1, "end": 1}
    )

    assert result.startswith("=== read_file metadata: sample.txt")
    assert "header and line numbers are not file content" in result
    assert result.endswith("   1: hello")


def test_patch_file_mismatch_explains_display_metadata(tmp_path):
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "# sample.txt\nhello",
            "new_text": "updated",
        },
    )

    assert "old_text must occur exactly once, found 0" in result
    assert "without the read_file metadata header or display line numbers" in result


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


def test_read_tool_output_reads_current_run_node_ref(tmp_path):
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


def test_read_tool_output_reads_cross_run_node_ref(tmp_path):
    agent = build_agent(tmp_path, [])
    run_dir = agent.run_store.run_dir("run_previous")
    output_path = run_dir / "tool_outputs" / "0002_run_shell.txt"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("pytest failed\n", encoding="utf-8")
    (run_dir / "task_graph.mmd").write_text(
        "flowchart TD\n"
        '  t002_run_shell["tool | error | run_shell pytest -q"]\n'
        "  %% t002_run_shell ref: tool_outputs/0002_run_shell.txt\n",
        encoding="utf-8",
    )

    result = agent.run_tool("read_tool_output", {"run_id": "run_previous", "node_id": "t002_run_shell"})

    assert result == "pytest failed\n"


def test_read_tool_output_resolves_ref_despite_long_label(tmp_path):
    # 回归：长命令让 label 触顶 220 字符截断时，ref 存在独立注释行仍可解析。
    agent = build_agent(tmp_path, [])
    state = agent.current_task_state = TaskState.create(
        run_id="run_long",
        task_id="task_long",
        user_request="Run the suite.",
    )
    agent.run_store.start_run(state)
    long_command = "uv run --with pytest python -m pytest " + " ".join(
        f"tests/test_module_{i}.py" for i in range(20)
    )
    content_ref = agent.run_store.save_tool_output(state, 3, "run_shell", "pytest output\n")
    agent.run_store.append_task_graph_tool(
        state, "t003_run_shell", "run_shell", {"command": long_command}, "ok", content_ref
    )

    result = agent.run_tool("read_tool_output", {"node_id": "t003_run_shell"})

    assert result == "pytest output\n"


def test_read_tool_output_rejects_ref_outside_tool_outputs(tmp_path):
    agent = build_agent(tmp_path, [])
    run_dir = agent.run_store.run_dir("run_previous")
    run_dir.mkdir(parents=True)
    (run_dir / "task_graph.mmd").write_text(
        "flowchart TD\n"
        '  t001_read_file["tool | ok | bad ref"]\n'
        "  %% t001_read_file ref: tool_outputs/../../README.md\n",
        encoding="utf-8",
    )

    result = agent.run_tool("read_tool_output", {"run_id": "run_previous", "node_id": "t001_read_file"})

    assert "invalid ref" in result


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
    assert agent._last_tool_result_metadata["tool_status"] == "rejected"
    assert agent._last_tool_result_metadata["delegate_outcome"] == {
        "requested_count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "items": [],
    }


def test_delegate_many_validates_each_task(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("delegate_many", {"tasks": [{"role": "explore", "task": ""}]})

    assert "tasks[1].task must not be empty" in result


def test_delegate_many_rejects_unknown_role(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool("delegate_many", {"tasks": [{"role": "builder", "task": "inspect README.md"}]})

    assert "unsupported delegate role: builder" in result


def test_delegate_tool_exception_keeps_structured_not_run_evidence(tmp_path):
    agent = build_agent(tmp_path, [])

    def fail(_args):
        raise RuntimeError("scheduler unavailable")

    agent.tools["delegate"]["run"] = fail
    result = agent.run_tool(
        "delegate", {"role": "explore", "task": "inspect README.md", "max_steps": 2}
    )

    assert result == "error: tool delegate failed: scheduler unavailable"
    assert agent._last_tool_result_metadata["tool_status"] == "error"
    assert agent._last_tool_result_metadata["delegate_outcome"] == {
        "requested_count": 1,
        "completed_count": 0,
        "failed_count": 1,
        "items": [
            {
                "index": 1,
                "role": "explore",
                "status": "not_run",
                "agent_id": "",
            }
        ],
    }


def test_delegate_child_failure_sets_partial_success_tool_status(tmp_path):
    agent = build_agent(tmp_path, [])

    def partial(_args):
        agent._delegate_outcome_metadata = {
            "requested_count": 2,
            "completed_count": 1,
            "failed_count": 1,
            "items": [
                {
                    "index": 1,
                    "role": "explore",
                    "status": "ok",
                    "agent_id": "child-1",
                    "child_status": "completed",
                    "stop_reason": "final_answer_returned",
                },
                {
                    "index": 2,
                    "role": "review",
                    "status": "error",
                    "agent_id": "",
                    "child_status": "",
                    "stop_reason": "",
                },
            ],
        }
        return "one child completed; one child failed"

    agent.tools["delegate_many"]["run"] = partial
    agent.run_tool(
        "delegate_many",
        {
            "tasks": [
                {"role": "explore", "task": "inspect", "max_steps": 1},
                {"role": "review", "task": "review", "max_steps": 1},
            ]
        },
    )

    assert agent._last_tool_result_metadata["tool_status"] == "partial_success"
    assert (
        agent._last_tool_result_metadata["tool_error_code"]
        == "delegate_partial_success"
    )


def test_responses_action_tools_are_strict_and_include_final(tmp_path):
    agent = build_agent(tmp_path, [])

    definitions = responses_action_tools(agent.tools)

    assert definitions[-1]["name"] == "submit_final"
    assert all(item["type"] == "function" and item["strict"] is True for item in definitions)
    for item in definitions:
        parameters = item["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])
    delegate_many = next(item for item in definitions if item["name"] == "delegate_many")
    task_schema = delegate_many["parameters"]["properties"]["tasks"]["items"]
    assert task_schema["additionalProperties"] is False
    assert set(task_schema["required"]) == {"role", "task", "max_steps"}
