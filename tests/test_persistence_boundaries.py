from pico import FakeModelClient, ModelAction, Pico, SessionStore, WorkspaceContext


def test_one_tool_run_has_stable_persistence_boundaries(tmp_path, monkeypatch):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    agent = Pico(
        FakeModelClient(
            [
                ModelAction.tool(
                    "read_file", {"path": "sample.txt", "start": 1, "end": 1}
                ),
                ModelAction.final("Done."),
            ]
        ),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        verification_command="",
    )
    counts = {"session": 0, "task_state": 0, "context": 0}
    event_types = []

    original_session_save = agent.session_store.save
    original_task_state = agent.run_store.write_task_state
    original_context = agent.run_store.append_context
    original_event = agent.run_store.append_event

    def save_session(session):
        counts["session"] += 1
        return original_session_save(session)

    def write_task_state(task_state):
        counts["task_state"] += 1
        return original_task_state(task_state)

    def append_context(run_id, entry):
        counts["context"] += 1
        return original_context(run_id, entry)

    def append_event(run_id, task_id, event_type, payload=None, **kwargs):
        event_types.append(event_type)
        return original_event(run_id, task_id, event_type, payload, **kwargs)

    monkeypatch.setattr(agent.session_store, "save", save_session)
    monkeypatch.setattr(agent.run_store, "write_task_state", write_task_state)
    monkeypatch.setattr(agent.run_store, "append_context", append_context)
    monkeypatch.setattr(agent.run_store, "append_event", append_event)

    assert agent.ask("Read sample.txt") == "Done."

    assert counts == {"session": 3, "task_state": 5, "context": 4}
    assert event_types == [
        "run_started",
        "prompt_built",
        "model_requested",
        "model_parsed",
        "operation_started",
        "operation_finished",
        "checkpoint_created",
        "prompt_built",
        "model_requested",
        "model_parsed",
        "checkpoint_created",
        "run_finished",
    ]
    assert [item["role"] for item in agent.session["history"]] == [
        "user",
        "tool",
        "assistant",
    ]
    persisted = agent.session_store.load(agent.session["id"])
    assert persisted["history"] == agent.session["history"]
    assert agent.run_store.replay(agent.current_task_state.run_id).summary()[
        "pending_operations"
    ] == []
