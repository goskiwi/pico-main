import pytest
from pathlib import Path

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, Workspace
from pico.contracts import ToolCall
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.task_state import TaskContract, WriteScope


def build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n")
    runtime_workspace = Workspace.build(tmp_path)
    return Pico.create(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(mode="auto", verification_command=""),
        **kwargs,
        session_store=SessionStore(tmp_path / ".pico/sessions"),
    )


def run_active(agent, call):
    run_log = agent.run.run_log
    if run_log is None:
        run_log = RunLog(
            "run_safety_test",
            agent.session.id,
            agent.dependencies.run_store,
        )
        run_log.append_user(
            TaskContract(
                goal="Exercise path safety",
                write_scope=WriteScope("workspace"),
                verify_changes=False,
            )
        )
        agent.run.run_log = run_log
        agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    surface = agent.tools.resolve_surface()
    group = run_log.append_tool_calls((call,))
    return agent.tools.execute_pending_group(group.event_id, surface)[0]


def edit_call(path, revision):
    return ToolCall("edit_file", {"path": path, "old_text": "before", "new_text": "after",
                                 "expected_revision": revision}, "edit")


def test_edit_reads_one_original_and_reuses_it_for_backup(tmp_path, monkeypatch):
    from pico.mutations import file_revision

    agent = build_agent(tmp_path)
    target = tmp_path / "subject.txt"
    target.write_bytes(b"before\r\nuntouched\r\n")
    revision = file_revision(target)
    reads = []
    original_open = Path.open
    def measured(path, *args, **kwargs):
        if path == target and (args[0] if args else kwargs.get("mode")) == "rb":
            reads.append(path)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", measured)
    outcome = run_active(agent, edit_call("subject.txt", revision))
    assert outcome.status == "success"
    assert len(reads) == 3  # Original, pre-replace recheck, post-write observation.
    started = next(e for e in agent.run.run_log.events if e.kind == "tool_started")
    effect = started.payload["potential_effects"][0]
    _descriptor, data = agent.dependencies.artifacts.read_internal(
        agent.run.projection.run_id, effect["before_artifact_id"], expected_kind="workspace_preimage",
    )
    assert data == b"before\r\nuntouched\r\n"
    assert effect["before_state"] == revision
    assert target.read_bytes() == b"after\r\nuntouched\r\n"


@pytest.mark.parametrize("failure", ["backup", "started"])
def test_edit_does_not_write_if_preimage_or_intent_persistence_fails(tmp_path, monkeypatch, failure):
    from pico.mutations import file_revision

    agent = build_agent(tmp_path)
    target = tmp_path / "subject.txt"
    target.write_text("before")
    call = edit_call("subject.txt", file_revision(target))
    def fail(*_args, **_kwargs):
        raise OSError("injected persistence failure")
    if failure == "backup":
        monkeypatch.setattr(agent.dependencies.artifacts, "write_workspace_preimage", fail)
        assert run_active(agent, call).execution_state == "not_started"
    else:
        monkeypatch.setattr(RunLog, "append_tool_started", fail)
        with pytest.raises(OSError, match="injected"):
            run_active(agent, call)
    assert target.read_text() == "before"
    assert not any(e.kind == "tool_started" for e in agent.run.run_log.events)


def test_edit_commit_recheck_preserves_external_save(tmp_path, monkeypatch):
    from pico.mutations import file_revision

    agent = build_agent(tmp_path)
    target = tmp_path / "subject.txt"
    target.write_text("before")
    call = edit_call("subject.txt", file_revision(target))
    commit = agent.dependencies.mutations._commit
    def external_save(*args):
        target.write_text("external update")
        return commit(*args)
    monkeypatch.setattr(agent.dependencies.mutations, "_commit", external_save)
    outcome = run_active(agent, call)
    assert outcome.failure.code == "revision_conflict"
    assert outcome.side_effect_state == "none"
    assert target.read_text() == "external update"


def test_edit_after_write_without_result_can_be_reconciled(tmp_path, monkeypatch):
    from pico.mutations import file_revision
    from pico.run_lifecycle import RunLifecycle

    agent = build_agent(tmp_path)
    RunLifecycle(agent).initialize("Edit subject")
    target = tmp_path / "subject.txt"
    target.write_text("before")
    call = edit_call("subject.txt", file_revision(target))
    def fail(*_args, **_kwargs):
        raise OSError("result not persisted")
    with monkeypatch.context() as scoped:
        scoped.setattr(RunLog, "append_tool_result", fail)
        with pytest.raises(OSError, match="result not persisted"):
            run_active(agent, call)
    assert target.read_text() == "after"
    resumed = Pico.resume(FakeModelClient([]), agent.workspace, config=agent.config,
                          session=agent.session.store.load(agent.session.id))
    resumed.tools.reconcile_interrupted()
    assert not resumed.run.run_log.pending_tool_calls()
    assert resumed.run.evidence.changed_paths == ["subject.txt"]
    assert target.read_text() == "after"


def test_failed_child_has_one_error_source_and_replays(tmp_path):
    agent = build_agent(tmp_path, subagent_model_client_factory=lambda _spec: FakeModelClient([]))
    call = ToolCall("delegate", {"role": "explore", "task": "Inspect README", "allowed_write_paths": []})
    outcome = run_active(agent, call)
    assert outcome.status == "error"
    assert outcome.failure.detail
    assert "error" not in outcome.structured
    replayed = agent.dependencies.run_store.replay(agent.run.projection.run_id)
    child = replayed.children.record(outcome.structured["child_id"])
    assert child.result.error == outcome.failure.detail


def test_workspace_and_symlink_escape_are_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("secret")
    agent = build_agent(tmp_path)
    assert agent.tools.execute_manual(
        "read_file", {"path": "../" + outside.name}
    ).status == "rejected"
    (tmp_path / "link").symlink_to(outside)
    assert agent.tools.execute_manual("read_file", {"path": "link"}).status == (
        "rejected"
    )


def test_file_tools_reject_git_and_pico_internal_paths(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("internal\n", encoding="utf-8")
    agent = build_agent(tmp_path)

    read_git = agent.tools.execute_manual(
        "read_file",
        {"path": ".git/config", "start_line": 1, "end_line": 10},
    )
    write_pico = run_active(
        agent,
        ToolCall(
            "write_file",
            {"path": ".pico/injected.txt", "content": "injected\n"},
            "call_write_pico",
        ),
    )
    write_gitignore = run_active(
        agent,
        ToolCall(
            "write_file",
            {"path": ".gitignore", "content": ".pico/\n"},
            "call_write_gitignore",
        ),
    )

    assert read_git.status == "rejected"
    assert write_pico.status == "rejected"
    assert not (tmp_path / ".pico" / "injected.txt").exists()
    assert write_gitignore.status == "success"
