import json
import os
from unittest.mock import patch

from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.task_state import TaskState
from tests.fakes import FakeModelClient, final_action, tool_action_json
from tests.helpers import build_agent, build_workspace


def test_successful_run_persists_auditable_artifacts(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            tool_action_json(
                '{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}'
            ),
            final_action("Finished."),
        ],
    )

    assert agent.ask("Do the thing") == "Finished."

    run_dir = next(path for path in (tmp_path / ".pico" / "runs").iterdir() if path.is_dir())
    task_state = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    trace = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert {
        "task_state.json",
        "trace.jsonl",
        "report.json",
        "task.mmd",
        "offload.jsonl",
        "refs",
        "undo",
    } <= {path.name for path in run_dir.iterdir()}
    assert task_state["stop_reason"] == "final_answer_returned"
    assert report["summary"]["tools"] == ["read_file"]
    assert report["tool_audit"][0]["status"] == "ok"
    assert trace[0]["event"] == "run_started"
    assert trace[-1]["event"] == "run_finished"
    assert any(event["event"] == "tool_executed" for event in trace)
    task_canvas = (run_dir / "task.mmd").read_text(encoding="utf-8")
    index = json.loads((tmp_path / ".pico" / "runs" / "index.json").read_text(encoding="utf-8"))
    assert 'G["goal | done | Do the thing"]' in task_canvas
    assert len(index) == 1
    assert index[0]["status"] == "completed"
    assert index[0]["latest_node_id"] == "N001_read_file"
    assert index[0]["task_canvas_path"] == str(run_dir / "task.mmd")
    assert index[0]["offload_path"] == str(run_dir / "offload.jsonl")


def test_trace_report_session_and_tool_output_redact_secrets(tmp_path):
    secret = "sk-test-secret-123"
    with patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=True):
        agent = build_agent(
            tmp_path,
            [
                tool_action_json(
                    '{"name":"run_shell","args":{"command":'
                    '"printf \'%s\' \'sk-test-secret-123\'","timeout":20}}'
                ),
                final_action("Masked."),
            ],
        )
        assert agent.ask("Mask the secret") == "Masked."

    run_dir = next(path for path in (tmp_path / ".pico" / "runs").iterdir() if path.is_dir())
    persisted_text = "\n".join(
        [
            (run_dir / "trace.jsonl").read_text(encoding="utf-8"),
            (run_dir / "report.json").read_text(encoding="utf-8"),
            agent.session_path.read_text(encoding="utf-8"),
            *[
                path.read_text(encoding="utf-8")
                for path in (run_dir / "refs").glob("*.txt")
            ],
        ]
    )

    assert secret not in persisted_text
    assert "<redacted>" in persisted_text


def test_model_error_is_persisted_as_a_failed_run(tmp_path):
    workspace = build_workspace(tmp_path)
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )

    answer = agent.ask("Trigger model failure")

    assert answer.startswith("Stopped after model error: RuntimeError:")
    task_state = json.loads(
        agent.run_store.task_state_path(agent.current_task_state.run_id).read_text(
            encoding="utf-8"
        )
    )
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert task_state["status"] == "failed"
    assert task_state["stop_reason"] == "model_error"
    assert report["status"] == "failed"


def test_task_canvas_folds_old_steps_into_drill_down_phases(tmp_path):
    agent = build_agent(tmp_path, [])
    state = agent.current_task_state = TaskState.create(
        run_id="run_folded",
        task_id="task_folded",
        user_request="Inspect a long task.",
    )
    agent.run_store.start_run(state)

    for index in range(1, 7):
        node_id = f"N{index:03d}_read_file"
        result_ref = agent.run_store.save_reference(
            state,
            index,
            "read_file",
            f"tool output {index}",
        )
        agent.run_store.append_offload_event(
            state,
            node_id=node_id,
            tool_name="read_file",
            args={"path": f"file-{index}.txt"},
            summary=f"Read file-{index}.txt",
            status="done",
            result_ref=result_ref,
        )
        agent.run_store.append_task_node(
            state,
            node_id=node_id,
            summary=f"Read file-{index}.txt",
            status="done",
            result_ref=result_ref,
        )

    fold = agent.run_store.fold_task_canvas(
        state,
        token_counter=agent.count_tokens,
        max_active_nodes=4,
        retain_nodes=2,
        max_tokens=10_000,
    )

    active_canvas = agent.run_tool("read_task_canvas", {})
    phase_canvas = agent.run_tool("read_task_canvas", {"phase_id": "phase_001"})
    phase_index = json.loads(
        (agent.run_store.phases_dir(state.run_id) / "index.json").read_text(
            encoding="utf-8"
        )
    )

    assert fold["folded"] is True
    assert fold["archived_node_ids"] == [
        "N001_read_file",
        "N002_read_file",
        "N003_read_file",
        "N004_read_file",
    ]
    assert "archive | done | 1 phases / 4 task steps" in active_canvas
    assert "N001_read_file" not in active_canvas
    assert "N005_read_file" in active_canvas
    assert "Task phase phase_001:" in phase_canvas
    assert "N001_read_file" in phase_canvas
    assert phase_index[0]["path"] == "phases/phase_001.mmd"
