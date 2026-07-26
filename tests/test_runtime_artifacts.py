import json
import os
from io import StringIO
from unittest.mock import patch

from pico.runtime import Pico
from pico.trace_events import TRACE_EVENT_NAMES, TraceSink
from pico.session_store import SessionStore
from tests.fakes import FakeModelClient, final_action, tool_action_json
from tests.helpers import build_agent, build_workspace


def test_successful_run_persists_auditable_artifacts(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    live_trace = StringIO()
    agent = build_agent(
        tmp_path,
        [
            tool_action_json(
                '{"name":"read_file","args":{"files":[{"path":"hello.txt","start":1,"end":2}]}}'
            ),
            final_action("Finished."),
        ],
        trace_sink=TraceSink("jsonl", live_trace),
    )

    assert agent.ask("Do the thing") == "Finished."

    run_dir = next(path for path in (tmp_path / ".pico" / "runs").iterdir() if path.is_dir())
    task_state = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    session = json.loads(agent.session_path.read_text(encoding="utf-8"))
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
    assert "history" not in session
    assert report["tool_audit"][0]["status"] == "ok"
    assert trace[0]["event"] == "model_start"
    assert trace[-1]["event"] == "run_end"
    assert any(event["event"] == "tool_end" for event in trace)
    assert {event["event"] for event in trace} <= TRACE_EVENT_NAMES
    assert [event["seq"] for event in trace] == list(range(1, len(trace) + 1))
    assert {event["run_id"] for event in trace} == {task_state["run_id"]}
    assert {event["task_id"] for event in trace} == {task_state["task_id"]}
    assert [json.loads(line) for line in live_trace.getvalue().splitlines()] == trace
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
