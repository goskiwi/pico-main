import json
import os
from unittest.mock import patch

from pico.runtime import Pico
from pico.session_store import SessionStore
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
        "task_graph.mmd",
        "tool_outputs",
        "undo",
    } <= {path.name for path in run_dir.iterdir()}
    assert task_state["stop_reason"] == "final_answer_returned"
    assert report["summary"]["tools"] == ["read_file"]
    assert report["tool_audit"][0]["status"] == "ok"
    assert trace[0]["event"] == "run_started"
    assert trace[-1]["event"] == "run_finished"
    assert any(event["event"] == "tool_executed" for event in trace)


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
                for path in (run_dir / "tool_outputs").glob("*.txt")
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
