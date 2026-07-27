from pico import checkpoints, memory
from pico.task_state import TaskState
from pico.tool_runtime import record_process_note_for_tool
from tests.helpers import build_agent


def _task_state(label):
    return TaskState.create(
        run_id=f"run_{label}",
        task_id=f"task_{label}",
        user_request=label,
    )


def test_session_memory_keeps_fresh_file_summaries_and_process_notes_separate(tmp_path):
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

    memory_text = agent.memory_text()
    assert "module.py: 1: before = 1" in memory_text
    assert "Process Notes" in memory_text
    assert "run_shell rejected" in memory_text
    assert all(note["kind"] == "process" for note in agent.memory.state["episodic_notes"])

    agent.run_tool(
        "patch_file",
        {"path": "module.py", "old_text": "before = 1", "new_text": "after = 2"},
    )

    assert "module.py" not in agent.memory.state["file_summaries"]
    assert "before = 1" not in agent.memory_text()
    assert "run_shell rejected" in agent.memory_text()


def test_session_normalization_drops_legacy_file_read_notes(tmp_path):
    state = memory.default_memory_state()
    state["episodic_notes"] = [
        {
            "text": "stale source fact",
            "tags": ["module.py"],
            "source": "module.py",
            "created_at": "2026-07-27T00:00:00+00:00",
            "note_index": 0,
            "kind": "episodic",
        }
    ]

    restored = memory.LayeredMemory(state, workspace_root=tmp_path)

    assert restored.to_dict()["episodic_notes"] == []


def test_session_keeps_only_the_latest_resumable_checkpoint(tmp_path):
    agent = build_agent(tmp_path, [])
    first = checkpoints.create_checkpoint(agent, _task_state("first"), "first task", "tool_executed")
    second = checkpoints.create_checkpoint(agent, _task_state("second"), "second task", "final_answer")

    checkpoint_state = agent.session["checkpoints"]
    assert checkpoint_state["current_id"] == second["checkpoint_id"]
    assert checkpoint_state["items"] == {second["checkpoint_id"]: second}
    assert first["checkpoint_id"] not in checkpoint_state["items"]
    assert "parent_checkpoint_id" not in second
    assert checkpoints.current_checkpoint(agent) == second
