from dataclasses import replace

import pytest

from pico import FakeModelClient, Pico, PicoConfig, Session, SessionStore, Workspace
from pico.agent_loop import AgentLoop
from pico.context_manager import ContextBudgetExceeded


def test_session_create_and_load_return_ready_session_objects(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    created = store.create(tmp_path)
    assert isinstance(created, Session)
    assert created.path.is_file()
    created.set_active_run("run_saved")

    loaded = store.load(created.id)
    assert isinstance(loaded, Session)
    assert loaded.id == created.id
    assert loaded.workspace_root == tmp_path.resolve()
    assert loaded.active_run_id == "run_saved"
    assert loaded.path == created.path


def test_failed_session_save_does_not_advance_memory_or_disk(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path)
    session.set_active_run("run_first")

    def fail(*_args):
        raise OSError("disk failure")

    monkeypatch.setattr("pico.session_store.atomic_write_json", fail)
    with pytest.raises(OSError, match="disk failure"):
        session.set_active_run("run_second")
    assert session.active_run_id == "run_first"
    assert store.load(session.id).active_run_id == "run_first"


def test_pico_uses_prepared_session_and_config_without_rewrapping(tmp_path):
    workspace = Workspace.build(tmp_path)
    session = SessionStore(tmp_path / "sessions").create(workspace.root)
    config = PicoConfig(mode="auto")
    before = session.path.stat().st_mtime_ns
    agent = Pico(FakeModelClient([]), workspace, session, config=config)
    assert agent.session is session
    assert agent.config is config
    assert session.path.stat().st_mtime_ns == before


def test_session_from_another_workspace_is_rejected(tmp_path):
    workspace = Workspace.build(tmp_path)
    session = SessionStore(tmp_path / "sessions").create(tmp_path / "other")
    with pytest.raises(ValueError, match="workspace does not match"):
        Pico(FakeModelClient([]), workspace, session)


def test_prompt_cache_holds_tool_names_not_diagnostic_metadata(tmp_path, monkeypatch):
    workspace = Workspace.build(tmp_path)
    session = SessionStore(tmp_path / "sessions").create(workspace.root)
    agent = Pico(FakeModelClient([]), workspace, session, config=PicoConfig(mode="ask"))
    build = agent.prompt.build
    diagnostics = []

    def observed_build(*args, **kwargs):
        prompt, metadata = build(*args, **kwargs)
        diagnostics.append(metadata)
        return prompt, metadata

    monkeypatch.setattr(agent.prompt, "build", observed_build)
    loop = AgentLoop(agent)
    state = loop.lifecycle.initialize("Inspect")
    surface = loop._resolve_action_tool_surface()
    prompt = loop._prepare_prompt(state, surface)
    assert state.prompt_snapshot == (prompt, surface.names)
    assert "sections" in diagnostics[0]
    assert "tool_names" not in diagnostics[0]
    assert loop._prepare_prompt(state, surface) is prompt
    assert len(diagnostics) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "invalid"},
        {"max_new_tokens": 0},
        {"max_agent_turns": 0},
        {"max_tool_executions": 0},
        {"turn_timeout_seconds": 0},
        {"verification_command": None},
        {"compaction_reserve_tokens": 1},
        {"allowed_write_paths": ("a.py", "a.py")},
        {"allowed_tools": ()},
    ],
)
def test_config_is_validated_at_construction(options):
    with pytest.raises((TypeError, ValueError)):
        PicoConfig(**options)


def test_config_replace_normalizes_and_validates_without_changing_original():
    config = PicoConfig(max_agent_turns="5", secret_env_names={"api_key"})
    assert config.max_agent_turns == 5
    assert config.secret_env_names == frozenset({"API_KEY"})
    updated = replace(config, max_agent_turns="7")
    assert updated.max_agent_turns == 7
    with pytest.raises(ValueError):
        replace(config, max_agent_turns=0)
    with pytest.raises(TypeError):
        replace(config, unknown_option=True)
    assert config.max_agent_turns == 5


def test_replaced_config_is_used_by_context_budget(tmp_path):
    workspace = Workspace.build(tmp_path)
    agent = Pico(
        FakeModelClient([]),
        workspace,
        SessionStore(tmp_path / "sessions").create(workspace.root),
    )
    agent.config = replace(
        agent.config,
        provider_context_limit_tokens=8000,
        compaction_reserve_tokens=2000,
        compaction_keep_recent_tokens=6000,
    )
    assert agent.prompt._context.total_budget == 8000
    with pytest.raises(ContextBudgetExceeded):
        agent.prompt.build("example " * 10000)
