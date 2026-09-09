import subprocess

import pytest
from pathlib import Path
from dataclasses import replace

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


@pytest.mark.parametrize("tool_name", ["edit_file", "write_file"])
def test_changed_mutation_target_does_not_claim_external_effect(tmp_path, monkeypatch, tool_name):
    from pico.delivery import build_final_diff
    from pico.mutations import file_revision

    agent = build_agent(tmp_path)
    target = tmp_path / "subject.txt"
    external = tmp_path / "external.txt"
    external.write_text("external update")
    if tool_name == "edit_file":
        target.write_text("before")
        call = edit_call("subject.txt", file_revision(target))
    else:
        call = ToolCall("write_file", {"path": "subject.txt", "content": "after"}, "write")
    tool = agent.tools.registry[tool_name]
    runner = tool["run"]

    def external_swap(*args, **kwargs):
        assert any(event.kind == "tool_started" for event in agent.run.run_log.events)
        target.unlink(missing_ok=True)
        target.symlink_to(external)
        return runner(*args, **kwargs)

    monkeypatch.setitem(tool, "run", external_swap)
    outcome = run_active(agent, call)

    assert outcome.failure.code == "mutation_target_changed"
    assert outcome.side_effect_state == "none"
    assert outcome.affected_paths == ()
    assert agent.run.evidence.effects == []
    assert not build_final_diff(agent).artifact_id
    replayed = agent.dependencies.run_store.replay(agent.run.projection.run_id)
    assert replayed.evidence.effects == []
    assert target.is_symlink()
    assert external.read_text() == "external update"


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


def test_scope_is_checked_before_approval_and_plan_is_created_once(tmp_path):
    approvals, plans = [], []
    agent = build_agent(tmp_path, approval_handler=lambda *args: approvals.append(args) or True)
    agent.config = replace(agent.config, mode="code", allowed_write_paths=("allowed.txt",))
    planner = agent.tools.registry["write_file"]["plan"]
    def counted(*args):
        plan = planner(*args)
        plans.append(plan)
        return plan
    agent.tools.registry["write_file"]["plan"] = counted
    denied = run_active(agent, ToolCall("write_file", {"path": "forbidden.txt", "content": "no"}, "deny"))
    assert denied.failure.code == "write_scope_denied"
    assert not approvals
    allowed = run_active(agent, ToolCall("write_file", {"path": "allowed.txt", "content": "yes"}, "allow"))
    assert allowed.status == "success"
    assert len(plans) == 2  # Once per call, not again after approval.
    assert approvals[0][2].paths == (("allowed.txt", tmp_path / "allowed.txt"),)
    assert (tmp_path / "allowed.txt").read_text() == "yes"
    assert not (tmp_path / "forbidden.txt").exists()


@pytest.mark.parametrize("change", ["target", "permission"])
def test_approval_rechecks_targets_and_permissions(tmp_path, change):
    from pico.mutations import file_revision

    a, b, alias = (tmp_path / name for name in ("a.txt", "b.txt", "alias.txt"))
    a.write_text("before")
    b.write_text("before")
    alias.symlink_to(a)
    def approve(name, args, plan):
        assert plan.paths == (("a.txt", a),)
        if change == "target":
            alias.unlink()
            alias.symlink_to(b)
        else:
            agent.config = replace(agent.config, allowed_write_paths=())
        return True
    agent = build_agent(tmp_path, approval_handler=approve)
    agent.config = replace(agent.config, mode="code", allowed_write_paths=("a.txt", "b.txt"))
    outcome = run_active(agent, edit_call("alias.txt", file_revision(a)))
    assert outcome.failure.code == "approval_context_changed"
    assert outcome.execution_state == "not_started"
    assert a.read_text() == b.read_text() == "before"


@pytest.mark.parametrize("change_during_approval", [False, True])
def test_child_approval_compares_normalized_verification_command(tmp_path, change_during_approval):
    from pico import ModelAction
    from pico.run_lifecycle import RunLifecycle

    approvals = []

    def approve(name, args, plan):
        approvals.append(name)
        assert plan.operation["verification_command"] == "true"
        if change_during_approval:
            agent.config = replace(agent.config, verification_command=" false ")
        return True

    agent = build_agent(
        tmp_path,
        approval_handler=approve,
        subagent_model_client_factory=lambda _spec: FakeModelClient([
            ModelAction.tool("write_file", {"path": "added.txt", "content": "added\n"}),
            ModelAction.final("Added file."),
        ]),
    )
    (tmp_path / ".gitignore").write_text(".pico/\n")

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", "README.md", ".gitignore")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    agent.config = replace(agent.config, mode="code", verification_command=" true ")
    RunLifecycle(agent).initialize("Add file via Child")
    child = run_active(agent, ToolCall("delegate", {
        "role": "implement", "task": "Create added.txt", "allowed_write_paths": ["added.txt"],
    }, "delegate"))
    assert child.status == "success", child.failure
    result = run_active(agent, ToolCall("integrate_child", {"child_id": child.structured["child_id"]}, "integrate"))
    assert approvals == ["integrate_child"]
    if change_during_approval:
        assert result.failure.code == "approval_context_changed"
        assert result.execution_state == "not_started"
        assert not (tmp_path / "added.txt").exists()
    else:
        assert result.status == "success", result.failure
        assert (tmp_path / "added.txt").read_text() == "added\n"


def test_external_edit_is_observed_without_claiming_agent_effect_and_can_continue(tmp_path):
    from pico.mutations import file_revision
    from pico.run_lifecycle import RunLifecycle
    from pico.completion_controller import CompletionController
    from pico.delivery import build_final_diff
    from pico.run_projection import RunOutcome

    agent = build_agent(tmp_path)
    agent.config = replace(agent.config, verification_command="true")
    lifecycle = RunLifecycle(agent)
    lifecycle.initialize("Edit subject and preserve user changes")
    target = tmp_path / "subject.txt"
    target.write_text("before")
    assert run_active(agent, edit_call("subject.txt", file_revision(target))).status == "success"
    controller = CompletionController(agent)
    policy = controller.resolve_verification_policy()
    lifecycle.run_completion_verification(policy)
    assert controller.assess_verification("done", policy).allowed
    old_verification = agent.run.evidence.verifications[-1]
    target.write_text("after\nexternal addition\n")
    blocked = run_active(agent, ToolCall("edit_file", {"path": "subject.txt", "old_text": "after", "new_text": "final",
                         "expected_revision": file_revision(target)}, "before_read"))
    assert blocked.failure.code == "workspace_drift"
    assert blocked.failure.recovery == "retry_after_change"
    assert "read_file" in controller.assess("done", policy).instruction
    count = len(agent.run.evidence.effects)
    observed = run_active(agent, ToolCall("read_file", {"path": "subject.txt"}, "observe_external"))
    assert observed.structured["external_change_observed"] is True
    assert len(agent.run.evidence.effects) == count
    assert len(agent.run.evidence.external_changes) == 1
    assert agent.run.evidence.last_workspace_mutation_sequence > old_verification["finished_workspace_mutation_sequence"]
    assert not controller.assess_verification("done", policy).allowed
    next_call = ToolCall("edit_file", {"path": "subject.txt", "old_text": "after", "new_text": "final",
                         "expected_revision": observed.structured["revision"]}, "after_read")
    assert run_active(agent, next_call).status == "success"
    assert target.read_text() == "final\nexternal addition\n"
    lifecycle.run_completion_verification(policy)
    assert controller.assess_verification("done", policy).allowed
    diff = build_final_diff(agent)
    assert diff.external_paths == ("subject.txt",)
    _, data = agent.dependencies.artifacts.read_internal(agent.run.projection.run_id, diff.artifact_id)
    assert "not solely Agent-authored" in data.decode()
    assert "+external addition" in data.decode()
    with pytest.raises(ValueError, match="external changes"):
        agent.run.run_log.append_final("misattributed", replace(diff, external_paths=()))
    agent.run.run_log.append_final("done", diff)
    replayed = agent.dependencies.run_store.replay(agent.run.projection.run_id)
    assert replayed.evidence.external_changes == agent.run.evidence.external_changes
    assert RunOutcome(replayed).final_diff.external_paths == ("subject.txt",)


def test_external_deletion_can_be_acknowledged_then_recreated(tmp_path):
    from pico.mutations import file_revision

    agent = build_agent(tmp_path)
    target = tmp_path / "subject.txt"
    target.write_text("before")
    run_active(agent, edit_call("subject.txt", file_revision(target)))
    target.unlink()
    missing = run_active(agent, ToolCall("read_file", {"path": "subject.txt"}, "missing"))
    assert missing.failure.code == "missing_path"
    assert missing.structured["external_change_observed"]
    recreated = run_active(agent, ToolCall("write_file", {"path": "subject.txt", "content": "new"}, "recreate"))
    assert recreated.status == "success"
    assert agent.run.evidence.external_changes[0]["after_state"] == "absent"


@pytest.mark.parametrize("redirect_parent", [False, True])
def test_redirected_tracked_path_requires_restoring_target(tmp_path, redirect_parent):
    from pico.completion_controller import CompletionController
    from pico.mutations import file_revision

    agent = build_agent(tmp_path)
    tracked, other = tmp_path / "tracked", tmp_path / "other"
    tracked.mkdir()
    other.mkdir()
    target = tracked / "subject.txt"
    external = other / "subject.txt"
    target.write_text("before")
    external.write_text("external update")
    assert run_active(agent, edit_call("tracked/subject.txt", file_revision(target))).status == "success"
    original_revision = file_revision(target)
    redirected = tracked if redirect_parent else target
    backup = tmp_path / "original"
    redirected.rename(backup)
    redirected.symlink_to(other if redirect_parent else external, target_is_directory=redirect_parent)

    observed = run_active(agent, ToolCall("read_file", {"path": "tracked/subject.txt"}, "read_redirected"))
    assert observed.status == "success"
    assert observed.structured["path"] == "other/subject.txt"
    assert agent.run.evidence.change_set.files["tracked/subject.txt"].current_after_state == original_revision
    assert agent.run.evidence.external_changes == []
    controller = CompletionController(agent)
    policy = controller.resolve_verification_policy()
    assessment = controller.assess("done", policy)
    assert assessment.status == "workspace_drift"
    assert "Ask the user" in assessment.instruction
    assert "read_file" not in assessment.instruction
    assert assessment.evidence == "tracked/subject.txt"

    redirected.unlink()
    backup.rename(redirected)
    assert controller.assess("done", policy).allowed
    assert external.read_text() == "external update"


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
