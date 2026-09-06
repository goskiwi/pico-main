"""Optional isolated checks use the same admission and completion boundaries."""
import sys
from pathlib import Path

import pytest

from pico import FakeModelClient, ModelAction, Pico, PicoConfig, SessionStore, Workspace
from pico.command_runner import CommandResult
from pico.contracts import ToolCall


def agent_at(path, outputs, *, runner=None, mode="auto", verification=""):
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("demo\n")
    return Pico(FakeModelClient(outputs), Workspace.build(path),
                SessionStore(path / ".pico/sessions").create(path),
                config=PicoConfig(mode=mode, verification_command=verification),
                check_runner=runner)


def read_and_finish():
    return [ModelAction.tool("read_file", {"path": "README.md"}), ModelAction.final("done")]


def outcomes(agent):
    return [e.payload["outcome"] for e in agent.run.run_log.events if e.kind == "tool_result"]


def test_check_is_optional_and_never_exposed_in_ask(tmp_path):
    def check(**kwargs):
        raise AssertionError("Ask must not execute code")

    default = agent_at(tmp_path / "default", [])
    assert "run_check" not in {t["name"] for t in default.tools.model_action_tools()}
    ask = agent_at(tmp_path / "ask", [ModelAction.tool("run_check", {"code": "print(1)"}),
                                     *read_and_finish()], runner=check, mode="ask")
    assert "run_check" not in {t["name"] for t in ask.tools.model_action_tools()}
    ask.ask("Inspect")
    assert outcomes(ask)[0]["status"] == "rejected"
    assert outcomes(ask)[0]["execution_state"] == "not_started"


def test_check_failure_is_logged_and_receives_current_deadline(tmp_path):
    received = []
    def check(**kwargs):
        received.append(kwargs)
        return CommandResult(1, stdout="AssertionError: earlier records were cleared")

    agent = agent_at(tmp_path, [ModelAction.tool("run_check", {
        "code": "assert False", "kind": "python", "timeout_seconds": 5,
    }), *read_and_finish()], runner=check)
    agent.ask("Inspect behavior")
    result = outcomes(agent)[0]
    assert result["failure"]["code"] == "check_failed"
    assert result["side_effect_state"] == "none"
    assert received[0]["timeout_seconds"] == 5
    assert received[0]["execution_context"].remaining_seconds() > 0
    assert any(e.kind == "tool_started" and e.payload["tool_name"] == "run_check"
               for e in agent.run.run_log.events)
    replay = agent.dependencies.run_store.replay(agent.run.projection.run_id)
    assert replay.status == agent.run.projection.status


@pytest.mark.parametrize("args", [
    {"code": ""}, {"code": "x" * 16001},
    {"code": "pass", "kind": "shell"}, {"code": "pass", "timeout_seconds": 61},
])
def test_invalid_check_is_rejected_before_executor(tmp_path, args):
    def check(**kwargs):
        raise AssertionError("invalid check must not execute")

    agent = agent_at(tmp_path, [ModelAction.tool("run_check", args), *read_and_finish()],
                     runner=check)
    agent.ask("Inspect")
    assert outcomes(agent)[0]["execution_state"] == "not_started"


def test_check_cannot_join_observation_batch(tmp_path):
    def check(**kwargs):
        raise AssertionError("execution cannot be batched")

    calls = (ToolCall("run_check", {"code": "pass"}),
             ToolCall("read_file", {"path": "README.md"}))
    agent = agent_at(tmp_path, [ModelAction.tool_batch(calls), *read_and_finish()], runner=check)
    agent.ask("Inspect")
    assert all(r["execution_state"] == "not_started" for r in outcomes(agent)[:2])


def test_passing_diagnostic_cannot_replace_fixed_verification(tmp_path):
    def check(**kwargs):
        return CommandResult(0, stdout="diagnostic passed")

    agent = agent_at(tmp_path, [
        ModelAction.tool("write_file", {"path": "new.py", "content": "value = 1\n"}),
        ModelAction.tool("run_check", {"code": "assert True"}),
        ModelAction.final("done"), ModelAction.final("done"), ModelAction.final("done"),
    ], runner=check, verification=f"{sys.executable} -c 'raise SystemExit(1)'")
    result = agent.ask("Create new.py")
    assert result.status == "stopped"
    assert result.stop_reason == "completion_block_limit"
    verifications = [e.payload["status"] for e in agent.run.run_log.events
                     if e.kind == "verification_result"]
    assert verifications and all(status == "failed" for status in verifications)
    assert Path(tmp_path / "new.py").read_text() == "value = 1\n"
