import json
from pathlib import Path
import subprocess

import pico.agent_loop as agent_loop
import pico.tools as toolkit
import pytest
from pico.models import FakeModelClient
from pico.runtime import Pico
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext
from tests.helpers import UnitTestSandbox, build_agent


def test_delegate_uses_child_agent(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"role":"explore","task":"inspect README","max_steps":2}}</tool>',
            "<final>Child result.</final>",
            "<final>Parent incorporated the child result.</final>",
        ],
    )

    answer = agent.ask("Use delegation")

    assert answer == "Parent incorporated the child result."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result role=explore" in tool_events[0]["summary"]


def test_native_delegate_uses_independent_model_client(tmp_path):
    class NativeParentClient:
        supports_native_actions = True

        def __init__(self):
            self.fork_count = 0

        def fork_for_delegate(self):
            self.fork_count += 1
            return FakeModelClient(["<final>Child result.</final>"])

    agent = build_agent(tmp_path, [])
    parent_client = NativeParentClient()
    agent.model_client = parent_client

    result = toolkit.run_delegate_child(
        agent, {"role": "explore", "task": "inspect README", "max_steps": 2}
    )

    assert parent_client.fork_count == 1
    assert result["answer"] == "Child result."


def test_delegate_child_can_finalize_when_parent_requires_a_workspace_change(tmp_path):
    agent = build_agent(
        tmp_path,
        ["<final>Investigation complete.</final>"],
        feature_flags={"require_workspace_change": True},
    )

    result = toolkit.run_delegate_child(
        agent, {"role": "explore", "task": "inspect README", "max_steps": 2}
    )

    assert result["answer"] == "Investigation complete."
    assert result["status"] == "completed"
    assert result["stop_reason"] == "final_answer_returned"


def test_delegate_child_marks_session_and_disables_llm_memory_extraction(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project: Delegate findings must never become durable memory.</final>",
            "<final>Project: Delegate findings must never become durable memory.</final>",
            '{"memories":[{"type":"project","text":"must not run"}]}',
        ],
        feature_flags={"llm_memory_extract": True},
    )

    result = toolkit.run_delegate_child(
        agent,
        {
            "role": "explore",
            "task": "Remember this stable project fact after inspecting README.",
            "max_steps": 2,
        },
    )

    assert result["status"] == "completed"
    assert len(agent.model_client.prompts) == 1
    assert not (tmp_path / ".pico" / "memory").exists()
    assert agent.session["session_kind"] == "main"
    child_ids = [
        path.stem
        for path in agent.session_store.root.glob("*.json")
        if path.stem != agent.session["id"]
    ]
    assert len(child_ids) == 1
    child_session = agent.session_store.load(child_ids[0])
    assert child_session["session_kind"] == "delegate"
    assert child_session["agent_mode"] == "explore"
    assert child_session["parent_agent_id"] == agent.agent_id
    assert agent.session_store.latest() == agent.session["id"]
    child_task_states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in agent.run_store.root.glob("run_*/task_state.json")
    ]
    assert len(child_task_states) == 1
    assert child_task_states[0]["agent_mode"] == "explore"
    assert child_task_states[0]["parent_agent_id"] == agent.agent_id
    child_reports = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in agent.run_store.root.glob("run_*/report.json")
    ]
    assert len(child_reports) == 1
    assert child_reports[0]["durable_promotions"] == []
    assert child_reports[0]["llm_durable_promotions"] == []


def test_stopped_delegate_child_never_attempts_durable_memory_promotion(
    tmp_path, monkeypatch
):
    promotion_calls = []

    def record_promotion(*args):
        promotion_calls.append(args)
        return [], [], []

    monkeypatch.setattr(
        agent_loop.memory_runtime,
        "promote_durable_memory",
        record_promotion,
    )
    agent = build_agent(
        tmp_path,
        ['<tool>{"name":"list_files","args":{"path":"."}}</tool>'],
        feature_flags={"llm_memory_extract": True},
    )

    result = toolkit.run_delegate_child(
        agent,
        {
            "role": "explore",
            "task": "Remember a stable Project fact after inspecting the workspace.",
            "max_steps": 1,
        },
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"] == "step_limit_reached"
    assert promotion_calls == []
    assert not (tmp_path / ".pico" / "memory").exists()


def _nested_explicit_workspace(tmp_path):
    outer_repo = tmp_path / "outer-repo"
    outer_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer_repo, check=True)
    fixture = outer_repo / "fixture"
    fixture.mkdir()
    (fixture / "README.md").write_text("isolated fixture\n", encoding="utf-8")
    hidden_verifier = outer_repo / "parent_only_hidden_verifier.py"
    hidden_verifier.write_text("PARENT_ONLY_MARKER = True\n", encoding="utf-8")
    workspace = WorkspaceContext.build(fixture, repo_root_override=fixture)
    return outer_repo, fixture, hidden_verifier, workspace


def test_refresh_prefix_preserves_explicit_workspace_root_inside_parent_git_repo(
    tmp_path,
):
    outer_repo, fixture, hidden_verifier, workspace = _nested_explicit_workspace(
        tmp_path
    )
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(fixture / ".pico" / "sessions"),
        feature_flags={"llm_memory_extract": False, "llm_history_compaction": False},
        sandbox=UnitTestSandbox(fixture),
    )

    agent.refresh_prefix(force=True)

    assert Path(agent.workspace.repo_root) == fixture.resolve()
    assert agent.workspace.branch == "-"
    assert agent.workspace.status == "clean"
    assert agent.workspace.recent_commits == []
    assert f"- repo_root: {outer_repo.resolve()}\n" not in agent.prefix
    assert hidden_verifier.name not in agent.prefix


def test_refresh_prefix_rejects_workspace_root_drift(tmp_path):
    outer_repo, fixture, _, workspace = _nested_explicit_workspace(tmp_path)
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(fixture / ".pico" / "sessions"),
        feature_flags={"llm_memory_extract": False, "llm_history_compaction": False},
        sandbox=UnitTestSandbox(fixture),
    )
    agent.workspace = WorkspaceContext.build(outer_repo)

    with pytest.raises(RuntimeError, match="workspace root invariant violated"):
        agent.refresh_prefix(force=True)


def test_delegate_child_rejects_parent_workspace_root_drift(tmp_path):
    outer_repo, fixture, _, workspace = _nested_explicit_workspace(tmp_path)
    agent = Pico(
        model_client=FakeModelClient(["<final>must not run</final>"]),
        workspace=workspace,
        session_store=SessionStore(fixture / ".pico" / "sessions"),
        feature_flags={"llm_memory_extract": False, "llm_history_compaction": False},
        sandbox=UnitTestSandbox(fixture),
    )
    agent.workspace = WorkspaceContext.build(outer_repo)

    with pytest.raises(RuntimeError, match="workspace root invariant violated"):
        toolkit.run_delegate_child(
            agent,
            {"role": "explore", "task": "inspect files", "max_steps": 1},
        )

    assert agent.model_client.prompts == []


def test_delegate_child_cannot_search_parent_repo_from_explicit_workspace(tmp_path):
    _, fixture, hidden_verifier, workspace = _nested_explicit_workspace(tmp_path)
    agent = Pico(
        model_client=FakeModelClient(
            [
                '<tool>{"name":"search","args":{"pattern":"PARENT_ONLY_MARKER","path":"."}}</tool>',
                "<final>The marker is not present in the workspace.</final>",
            ]
        ),
        workspace=workspace,
        session_store=SessionStore(fixture / ".pico" / "sessions"),
        feature_flags={"llm_memory_extract": False, "llm_history_compaction": False},
        sandbox=UnitTestSandbox(fixture),
    )
    agent.refresh_prefix(force=True)

    result = toolkit.run_delegate_child(
        agent,
        {"role": "explore", "task": "search for the marker", "max_steps": 2},
    )

    run_dirs = list((fixture / ".pico" / "runs").glob("run_*"))
    assert result["status"] == "completed"
    assert len(run_dirs) == 1
    events = [
        json.loads(line)
        for line in (run_dirs[0] / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    search_event = next(
        event
        for event in events
        if event.get("event") == "tool_executed" and event.get("name") == "search"
    )
    assert search_event["result"] == "(no matches)"
    assert str(hidden_verifier.resolve()) not in search_event["result"]


def test_delegate_many_uses_multiple_child_agents(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate_many","args":{"tasks":[{"role":"explore","task":"inspect README","max_steps":2},{"role":"review","task":"review README","max_steps":2}]}}</tool>',
            "<final>Explore result.</final>",
            "<final>Review result.</final>",
            "<final>Parent incorporated the child results.</final>",
        ],
    )

    answer = agent.ask("Use multiple delegates")

    assert answer == "Parent incorporated the child results."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate_many"
    assert "delegate_many_result count=2" in tool_events[0]["summary"]
    assert "role=explore" in tool_events[0]["summary"]
    assert "Explore result." in tool_events[0]["summary"]
    assert "role=review" in tool_events[0]["summary"]
    assert "Review result." in tool_events[0]["summary"]
    parent_run_dir = agent.run_store.run_dir(agent.current_task_state)
    trace_events = [
        json.loads(line)
        for line in (parent_run_dir / "trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    delegate_event = next(
        event
        for event in trace_events
        if event.get("event") == "tool_executed"
        and event.get("name") == "delegate_many"
    )
    outcome = delegate_event["delegate_outcome"]
    assert outcome["requested_count"] == 2
    assert outcome["completed_count"] == 2
    assert outcome["failed_count"] == 0
    assert [item["status"] for item in outcome["items"]] == ["ok", "ok"]
    assert all(item["child_status"] == "completed" for item in outcome["items"])
    assert all(item["agent_id"] for item in outcome["items"])
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["tool_audit"][0]["delegate_outcome"] == outcome
