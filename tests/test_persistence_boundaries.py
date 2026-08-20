from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.run_journal import RunJournal


def test_one_tool_run_uses_one_recoverable_journal(tmp_path):
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
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )

    assert agent.ask("Read sample.txt") == "Done."

    run_id = agent.run.task_state.run_id
    run_dir = agent.services.run_store.run_dir(run_id)
    assert sorted(path.name for path in run_dir.iterdir()) == [
        "artifacts",
        "journal.jsonl",
    ]
    entries = agent.services.run_store.read_entries(run_id)
    assert {
        "user_message",
        "assistant_tool_call",
        "tool_started",
        "tool_result",
        "assistant_final",
    } <= {entry.kind for entry in entries}

    projection = agent.services.run_store.replay(run_id)
    assert projection.status == "completed"
    assert projection.final_answer == "Done."
    assert projection.summary()["pending_operations"] == []

    journal = RunJournal.restore(run_id, agent.services.run_store)
    assert journal.pending_call_id() == ""
    assert journal.active_entries()[-1].kind == "assistant_final"
    assert journal.active_entries()[-1].content == "Done."

    persisted = agent.session.store.load(agent.session.data["id"])
    assert persisted["active_run_id"] == ""
    report = agent.build_report(agent.run.task_state)
    assert report["run_id"] == run_id
    assert report["status"] == "completed"
    assert report["final_answer"] == "Done."
