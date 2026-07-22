import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import pico.run_store as run_store_module
from pico.run_store import RunStore
from pico.task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskState


def _process_index_update(root, run_id, start):
    """Widen the RMW race inside an independent process."""
    store = RunStore(root)
    state = TaskState.create(
        run_id=run_id,
        task_id=run_id.replace("run_", "task_", 1),
        user_request=f"Concurrent process task {run_id}",
    )
    original_read_text = Path.read_text
    index_path = store.index_path()

    def delayed_read_text(path, *args, **kwargs):
        value = original_read_text(path, *args, **kwargs)
        if path == index_path:
            time.sleep(0.02)
        return value

    Path.read_text = delayed_read_text
    start.wait(timeout=10)
    store.update_index(state)


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


def test_run_store_appends_trace_jsonl(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Trace the run.")
    store.start_run(state)

    store.append_trace(state, {"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"})
    store.append_trace(
        state.run_id,
        {
            "event": "prompt_built",
            "created_at": "2026-04-07T00:00:01+00:00",
            "prompt_metadata": {"prompt_chars": 128, "secret_env_count": 1},
        },
    )
    store.append_trace(state.run_id, {"event": "run_finished", "created_at": "2026-04-07T00:00:02+00:00"})

    lines = (store.trace_path(state.run_id)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["event"] == "prompt_built"
    assert json.loads(lines[2])["event"] == "run_finished"


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


def test_run_store_updates_cross_run_index_when_report_is_written(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_005", task_id="task_005", user_request="Continue the bug fix.")
    store.start_run(state)
    state.finish_success("Done.")

    store.write_report(
        state,
        {
            "task_id": state.task_id,
            "status": state.status,
            "stop_reason": state.stop_reason,
            "final_answer": state.final_answer,
        },
    )

    index = json.loads((store.root / "index.json").read_text(encoding="utf-8"))
    assert index == [
        {
            "run_id": "run_005",
            "task_id": "task_005",
            "task_goal": "Continue the bug fix.",
            "status": "completed",
            "stop_reason": STOP_REASON_FINAL_ANSWER_RETURNED,
            "agent_mode": "main",
            "parent_agent_id": "",
            "updated_at": index[0]["updated_at"],
            "task_graph_path": str(store.run_dir(state.run_id) / "task_graph.mmd"),
            "report_path": str(store.report_path(state.run_id)),
        }
    ]


def test_run_store_loads_recent_index_newest_first(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    (store.root / "index.json").write_text(
        json.dumps(
            [
                {"run_id": "run_old", "updated_at": "2026-04-07T09:00:00+00:00"},
                {"run_id": "run_new", "updated_at": "2026-04-07T10:00:00+00:00"},
                {"run_id": "run_middle", "updated_at": "2026-04-07T09:30:00+00:00"},
            ]
        ),
        encoding="utf-8",
    )

    recent = store.load_recent_index(limit=2)

    assert [item["run_id"] for item in recent] == ["run_new", "run_middle"]


def test_run_store_recent_index_excludes_delegate_runs_by_default(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    (store.root / "index.json").write_text(
        json.dumps(
            [
                {
                    "run_id": "run_legacy_main",
                    "updated_at": "2026-04-07T09:00:00+00:00",
                },
                {
                    "run_id": "run_main",
                    "agent_mode": "main",
                    "parent_agent_id": "",
                    "updated_at": "2026-04-07T10:00:00+00:00",
                },
                {
                    "run_id": "run_child",
                    "agent_mode": "review",
                    "parent_agent_id": "agent_parent",
                    "updated_at": "2026-04-07T11:00:00+00:00",
                },
            ]
        ),
        encoding="utf-8",
    )

    recent_main = store.load_recent_index(limit=2)
    recent_all = store.load_recent_index(limit=2, include_children=True)

    assert [item["run_id"] for item in recent_main] == [
        "run_main",
        "run_legacy_main",
    ]
    assert [item["run_id"] for item in recent_all] == ["run_child", "run_main"]


def test_run_store_tolerates_missing_final_report(tmp_path):
    store = RunStore(tmp_path / ".pico" / "runs")
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Crash before finalize.")

    store.start_run(state)
    store.append_trace(state, {"event": "run_started"})

    assert store.trace_path(state.run_id).exists()
    assert not store.report_path(state.run_id).exists()


def test_run_store_concurrent_index_updates_do_not_drop_entries(tmp_path, monkeypatch):
    root = tmp_path / ".pico" / "runs"
    stores = [RunStore(root) for _ in range(12)]
    states = [
        TaskState.create(
            run_id=f"run_{index:03d}",
            task_id=f"task_{index:03d}",
            user_request=f"Concurrent task {index}",
        )
        for index in range(len(stores))
    ]
    stores[0].index_path().write_text("[]\n", encoding="utf-8")

    # Widen the old implementation's read/modify/write race.  Correct code
    # serializes this delayed read across every RunStore for the same root.
    original_read_text = Path.read_text
    index_path = stores[0].index_path()

    def delayed_read_text(path, *args, **kwargs):
        if path == index_path:
            time.sleep(0.01)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", delayed_read_text)
    start = threading.Barrier(len(stores))

    def update(store, state):
        start.wait()
        store.update_index(state)

    with ThreadPoolExecutor(max_workers=len(stores)) as executor:
        futures = [
            executor.submit(update, store, state)
            for store, state in zip(stores, states)
        ]
        for future in futures:
            future.result()

    index = json.loads(original_read_text(index_path, encoding="utf-8"))
    assert {entry["run_id"] for entry in index} == {
        state.run_id for state in states
    }
    assert len(index) == len(states)
    assert not list(root.glob("index.json.*.tmp"))


@pytest.mark.skipif(
    run_store_module.fcntl is None,
    reason="cross-process RunStore locking uses POSIX fcntl",
)
def test_run_store_cross_process_index_updates_do_not_drop_entries(tmp_path):
    root = tmp_path / ".pico" / "runs"
    store = RunStore(root)
    store.index_path().write_text("[]\n", encoding="utf-8")
    process_count = 8
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(process_count)
    processes = [
        context.Process(
            target=_process_index_update,
            args=(root, f"run_{index:03d}", start),
        )
        for index in range(process_count)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0] * process_count
    index = json.loads(store.index_path().read_text(encoding="utf-8"))
    assert {entry["run_id"] for entry in index} == {
        f"run_{index:03d}" for index in range(process_count)
    }
    assert len(index) == process_count
