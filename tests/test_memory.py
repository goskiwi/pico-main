from pico.tools.runtime import record_process_note_for_tool
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
