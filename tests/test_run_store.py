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


def test_run_store_validates_once_then_appends_incrementally(tmp_path, monkeypatch):
    store = RunStore(tmp_path / ".pico" / "runs")
    decode_calls = 0
    original = store._decode_events

    def counted(run_id, text):
        nonlocal decode_calls
        decode_calls += 1
        return original(run_id, text)

    monkeypatch.setattr(store, "_decode_events", counted)
    for index in range(100):
        store.append_event("run_linear", "task_linear", "model_requested", {"attempts": index})

    assert decode_calls == 1
    assert store.event_cursor("run_linear").sequence == 100


def test_run_store_accepts_valid_tail_from_another_writer(tmp_path):
    root = tmp_path / ".pico" / "runs"
    first = RunStore(root)
    second = RunStore(root)
    first.append_event("run_shared", "task_shared", "run_started")
    second.append_event("run_shared", "task_shared", "model_requested", {"attempts": 1})
    first.append_event("run_shared", "task_shared", "run_finished", {"status": "completed"})

    events = first.read_events("run_shared")
    assert [event["sequence"] for event in events] == [1, 2, 3]


def test_run_store_fails_closed_when_event_file_is_truncated(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    store.append_event("run_tampered", "task_tampered", "run_started")
    path = store.events_path("run_tampered")
    path.write_bytes(path.read_bytes()[:-5])

    try:
        store.append_event("run_tampered", "task_tampered", "run_finished")
    except ValueError as exc:
        assert "truncated" in str(exc)
    else:
        raise AssertionError("truncated event log was accepted")


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
