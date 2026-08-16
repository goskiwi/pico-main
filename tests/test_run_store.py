import json

from pico.run_store import RunStore
from pico.task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskState


def test_run_store_creates_run_directory_and_state_file(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    run_dir = store.start_run(state)

    assert run_dir == store.run_dir(state.run_id)
    assert run_dir.exists()
    persisted = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task_001"
    assert persisted["run_id"] == "run_001"
    assert persisted["user_request"] == "Inspect the repo."


def test_run_store_appends_hash_chained_events(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Trace the run.")
    store.start_run(state)

    store.append_event(state.run_id, state.task_id, "run_started", {"user_request": state.user_request})
    store.append_event(state.run_id, state.task_id, "prompt_built", {"prompt_tokens": 128})
    store.append_event(state.run_id, state.task_id, "run_finished", {"status": "completed"})

    events = store.read_events(state.run_id)
    assert [event["event_type"] for event in events] == ["run_started", "prompt_built", "run_finished"]
    assert events[1]["previous_hash"] == events[0]["event_hash"]
    assert store.event_cursor(state.run_id).event_hash == events[-1]["event_hash"]


def test_run_store_writes_report_json(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_003", task_id="task_003", user_request="Report the run.")
    store.start_run(state)
    state.finish_success("Done.")

    store.write_task_state(state)
    store.write_report(state, {"task_state": state.to_dict(), "stop_reason": state.stop_reason})

    report = json.loads(store.report_path(state.run_id).read_text(encoding="utf-8"))
    assert report["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
    assert report["task_state"]["final_answer"] == "Done."


def test_run_store_tolerates_missing_final_report(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Crash before finalize.")

    store.start_run(state)
    store.append_event(state.run_id, state.task_id, "run_started")

    assert store.events_path(state.run_id).exists()
    assert not store.report_path(state.run_id).exists()
