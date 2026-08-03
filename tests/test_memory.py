from pico.tools.runtime import record_process_note_for_tool
from tests.fakes import final_action, tool_action_json
from tests.helpers import build_agent


def test_memory_keeps_fresh_file_facts_separate_from_process_feedback(tmp_path):
    (tmp_path / "module.py").write_text("before = 1\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    agent.run_tool(
        "read_file",
        {"files": [{"path": "module.py", "start": 1, "end": 1}]},
    )
    record_process_note_for_tool(
        agent,
        "run_shell",
        {"tool_status": "rejected", "affected_paths": ["module.py"]},
    )
    agent.run_tool(
        "patch_file",
        {"path": "module.py", "old_text": "before = 1", "new_text": "after = 2"},
    )

    assert "module.py" not in agent.memory.state["file_summaries"]
    assert "before = 1" not in agent.memory_text()
    assert "run_shell rejected" in agent.memory_text()


def test_session_memory_invalidates_file_summary_when_file_freshness_changes(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("before = 1\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    agent.run_tool(
        "read_file",
        {"files": [{"path": "module.py", "start": 1, "end": 1}]},
    )

    summary = agent.memory.state["file_summaries"]["module.py"]
    assert summary["freshness"]
    assert "before = 1" in agent.memory_text()

    path.write_text("after = 2\n", encoding="utf-8")

    assert agent.memory.invalidate_stale_file_summaries() == ["module.py"]
    assert "module.py" not in agent.memory.state["file_summaries"]
    assert "before = 1" not in agent.memory_text()
    assert agent.memory.state["working"]["recent_files"] == ["module.py"]


def test_duplicate_read_only_call_is_blocked_and_reuses_cached_evidence(tmp_path):
    (tmp_path / "module.py").write_text("answer = 42\n", encoding="utf-8")
    read_action = tool_action_json(
        '{"name":"read_file","args":{"files":[{"path":"module.py","start":1,"end":1}]}}'
    )
    agent = build_agent(
        tmp_path,
        [read_action, read_action, final_action("Used the saved evidence.")],
        max_steps=3,
    )
    read_calls = []
    recorded_results = []
    original_read = agent.tools["read_file"]["run"]

    def count_read_calls(args):
        result = original_read(args)
        read_calls.append(result)
        return result

    agent.tools["read_file"]["run"] = count_read_calls
    agent.model_client.record_action_result = (
        lambda action, result: recorded_results.append((action.name, result))
    )

    assert agent.ask("Inspect module.py twice.") == "Used the saved evidence."
    assert len(read_calls) == 1
    assert "answer = 42" in read_calls[0]
    assert [entry["status"] for entry in agent.tool_audit_log] == ["ok", "rejected"]
    assert agent.tool_audit_log[1]["error_code"] == "duplicate_read_only_call"
    assert "duplicate read-only call blocked" in agent.tool_audit_log[1]["result_preview"]
    assert [name for name, _ in recorded_results] == ["read_file", "read_file"]
    assert "answer = 42" in recorded_results[1][1]


def test_duplicate_read_only_call_is_reallowed_after_workspace_change(tmp_path):
    (tmp_path / "module.py").write_text("before = 1\n", encoding="utf-8")
    read_action = tool_action_json(
        '{"name":"read_file","args":{"files":[{"path":"module.py","start":1,"end":1}]}}'
    )
    agent = build_agent(
        tmp_path,
        [
            read_action,
            tool_action_json(
                '{"name":"write_file","args":{"path":"module.py","content":"after = 2\\n"}}'
            ),
            read_action,
            final_action("Re-read the changed file."),
        ],
        max_steps=4,
    )
    read_results = []
    original_read = agent.tools["read_file"]["run"]

    def record_read_result(args):
        result = original_read(args)
        read_results.append(result)
        return result

    agent.tools["read_file"]["run"] = record_read_result

    assert agent.ask("Read, update, then re-read module.py.") == "Re-read the changed file."
    assert [entry["status"] for entry in agent.tool_audit_log] == ["ok", "ok", "ok"]
    assert len(read_results) == 2
    assert "before = 1" in read_results[0]
    assert "after = 2" in read_results[1]
    assert "after = 2" in agent.memory.state["file_summaries"]["module.py"]["summary"]
