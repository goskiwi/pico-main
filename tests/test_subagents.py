import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    Workspace,
)
from pico.command_runner import CommandResult, CommandRunner
from pico.completion_controller import CompletionController
from pico.contracts import ToolCall
from pico.execution import ExecutionContext, ExecutionDeadlineExceeded
from pico.mutations import file_revision
from pico.run_log import RunLog
from pico.subagents.contracts import (
    ChildFailure,
    ChildPatch,
    ChildRecord,
    ChildSpec,
    ChildSuccess,
)
from pico.subagents.worktree import (
    GitWorktree,
    GitWorktreeError,
    _git,
)
from pico.task_state import TaskContract

CHILD_SUCCESS_FIELDS = {
    "child_id",
    "role",
    "status",
    "child_run_id",
    "result",
}


def test_child_git_consumes_parent_remaining_time(tmp_path, monkeypatch):
    from types import SimpleNamespace

    timeouts = []

    def run(_argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr("pico.subagents.worktree.subprocess.run", run)
    parent = ExecutionContext.root(max_seconds=0.25)
    assert _git(tmp_path, "status", execution_context=parent) == b"ok"
    assert 0 < timeouts[0] <= 0.25
    expired = ExecutionContext.root(max_seconds=0, deadline=0)
    with pytest.raises(ExecutionDeadlineExceeded):
        _git(tmp_path, "status", execution_context=expired)
    assert len(timeouts) == 1


def test_child_repository_error_preserves_git_failure(tmp_path, monkeypatch):
    parent, _ = build_parent(repository(tmp_path), lambda _spec: implement_client())

    def fail(*_args, **_kwargs):
        raise GitWorktreeError("Permission denied while reading .git")

    monkeypatch.setattr("pico.subagents.runner._git", fail)
    receipt = parent.dependencies.subagents.delegate("implement", "Create feature.py", ("feature.py",))
    assert receipt["status"] == "failed"
    assert "Permission denied while reading .git" in receipt["error"]


def git(root, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--quiet")
    git(root, "config", "user.email", "pico@example.test")
    git(root, "config", "user.name", "Pico Test")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    (root / ".gitignore").write_text(".pico/\n", encoding="utf-8")
    git(root, "add", "README.md", ".gitignore")
    git(root, "commit", "--quiet", "-m", "base")
    return root


class RecordingCommandRunner:
    def __init__(self, root, *, fail=False):
        self.root = Path(root)
        self.fail = fail
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), dict(kwargs)))
        if self.fail:
            return CommandResult(returncode=1, stderr="verification failed\n")
        return CommandResult(returncode=0, stdout="1 passed\n")


class RunnerFactory:
    def __init__(self, *, fail_integration=False):
        self.fail_integration = fail_integration
        self.runners = []

    def __call__(self, root):
        root = Path(root)
        runner = RecordingCommandRunner(
            root,
            fail=self.fail_integration and root.name.startswith("integration-"),
        )
        self.runners.append(runner)
        return runner


class MutatingRunnerFactory:
    def __init__(self, mutation):
        self.mutation = mutation

    def __call__(self, root):
        root = Path(root)
        mutation = self.mutation

        class MutatingRunner(RecordingCommandRunner):
            def run(self, argv, **kwargs):
                if self.root.name.startswith("integration-"):
                    mutation(self.root)
                return super().run(argv, **kwargs)

        return MutatingRunner(root)


def activate_parent(
    parent,
    *,
    allows_workspace_mutation=True,
    allowed_write_paths=None,
):
    run_log = RunLog(
        "run_parent",
        "task_parent",
        parent.session.id,
        parent.dependencies.run_store,
    )
    run_log.append_user(
        TaskContract(
            goal="Parent task",
            allows_workspace_mutation=allows_workspace_mutation,
            verify_changes=False,
            allowed_write_paths=allowed_write_paths,
        )
    )
    parent.run.projection = run_log.projection
    parent.run.run_log = run_log
    parent.run.execution_context = ExecutionContext.root(max_seconds=60)


def build_parent(
    root,
    child_factory,
    *,
    verification_command="verify",
    allowed_write_paths=None,
    allows_workspace_mutation=True,
    runner_factory=None,
):
    runner_factory = runner_factory or RunnerFactory()
    runtime_workspace = Workspace.build(root)
    parent = Pico(
        FakeModelClient([]),
        runtime_workspace,
        config=PicoConfig(
            mode="auto",
            verification_command=verification_command,
            allowed_write_paths=allowed_write_paths,
        ),
        command_runner_factory=runner_factory,
        subagent_model_client_factory=child_factory,
        session=SessionStore(root / ".pico" / "sessions").create(
            runtime_workspace.root
        ),
    )
    activate_parent(
        parent,
        allows_workspace_mutation=allows_workspace_mutation,
        allowed_write_paths=allowed_write_paths,
    )
    return parent, runner_factory


def run_active(parent, call):
    group = parent.run.run_log.append_tool_calls((call,))
    return parent.tools.execute_pending_group(group.event_id)[0]


def test_child_inherits_parent_parallel_tool_limit(tmp_path):
    parent, _runner = build_parent(
        repository(tmp_path), lambda _spec: explore_client()
    )
    parent.config = replace(parent.config, max_parallel_tools=2)
    record = ChildRecord(
        "child_0123456789ab",
        ChildSpec(role="explore", task="Inspect the repository"),
    )

    child = parent.dependencies.subagents._build_child(
        parent.run.projection.run_id, record
    )

    assert child.config.max_parallel_tools == 2


class DeadlineRecordingClient(FakeModelClient):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.request_timeouts = []

    def complete_action(self, *args, **kwargs):
        self.request_timeouts.append(kwargs.get("request_timeout"))
        return super().complete_action(*args, **kwargs)


def explore_client(
    answer="Findings: README.md contains demo.",
    *,
    client_type=FakeModelClient,
):
    return client_type(
        [
            ModelAction.tool(
                "read_file",
                {"path": "README.md", "start_line": 1, "end_line": 20},
            ),
            ModelAction.final(answer),
        ]
    )


def implement_client(path="feature.py"):
    return FakeModelClient(
        [
            ModelAction.tool(
                "write_file",
                {"path": path, "content": "feature\n"},
            ),
            ModelAction.final(f"implemented {path}"),
        ]
    )


def delegate_implementation(parent, path="feature.py"):
    return run_active(
        parent,
        ToolCall(
            "delegate",
            {
                "role": "implement",
                "task": f"Create {path}",
                "allowed_write_paths": [path],
            },
        ),
    ).structured


def test_restored_child_constraints_do_not_require_an_execution_factory(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root, lambda _spec: implement_client(), allowed_write_paths=("feature.py",)
    )
    receipt = delegate_implementation(parent)
    resumed = Pico(
        FakeModelClient([]), Workspace.build(root), parent.session, config=parent.config
    )
    assert resumed.dependencies.subagents is None
    assert {"delegate", "integrate_child"} <= set(
        resumed.tools.history_projectors()
    )
    record = resumed.run.projection.children.record(receipt["child_id"])
    assert record.completed().patch.integrated is False
    assert CompletionController(resumed).assess("done").status == "subtasks_incomplete"


def test_explore_creates_distinct_ask_mode_non_recursive_children(tmp_path):
    clients = []

    def child_factory(spec):
        assert spec.role == "explore"
        client = explore_client(
            f"handoff {len(clients) + 1}",
            client_type=DeadlineRecordingClient,
        )
        clients.append(client)
        return client

    parent, _ = build_parent(
        repository(tmp_path),
        child_factory,
    )
    outcomes = [
        run_active(
            parent,
            ToolCall(
                "delegate",
                {
                    "role": "explore",
                    "task": f"Inspect README pass {index}",
                    "allowed_write_paths": [],
                },
                f"call_explore_{index}",
            ),
        )
        for index in (1, 2)
    ]
    receipts = [outcome.structured for outcome in outcomes]

    assert all(outcome.status == "success" for outcome in outcomes)
    assert all(set(receipt) == CHILD_SUCCESS_FIELDS for receipt in receipts)
    assert all(receipt["status"] == "completed" for receipt in receipts)
    assert receipts[0]["child_id"] != receipts[1]["child_id"]
    assert receipts[0]["child_run_id"] != receipts[1]["child_run_id"]

    manager = parent.dependencies.subagents
    for receipt, client in zip(receipts, clients):
        record = parent.run.projection.children.record(receipt["child_id"])
        projection = manager._child_projection(parent.run.projection.run_id, record)
        child_tools = set(client.action_tool_surfaces[0])
        assert projection.contract.allows_workspace_mutation is False
        assert {"delegate", "integrate_child", "write_file", "edit_file"}.isdisjoint(
            child_tools
        )
        assert all(0 < timeout <= 60 for timeout in client.request_timeouts)


def test_failed_child_keeps_run_id_without_empty_success_fields(tmp_path):
    parent, _ = build_parent(repository(tmp_path), lambda _spec: FakeModelClient([]))

    receipt = parent.dependencies.subagents.delegate("explore", "Inspect README")

    assert receipt["status"] == "failed"
    assert receipt["child_run_id"]
    assert receipt["error"]
    assert "result" not in receipt
    assert "patch" not in receipt


def test_child_result_types_reject_contradictory_success_shapes():
    spec = ChildSpec(role="explore", task="Inspect")
    patch = ChildPatch(("file.py",), "digest")
    with pytest.raises(ValueError, match="Explore Child"):
        ChildRecord("child_example", spec, result=ChildSuccess("run_child", patch))
    with pytest.raises(ValueError, match="requires an error"):
        ChildFailure("")


def test_implement_requires_verifier_and_clean_git(tmp_path):
    root = repository(tmp_path)
    calls = []

    def child_factory(_spec):
        calls.append(True)
        return implement_client()

    no_verifier, _ = build_parent(root, child_factory, verification_command="")
    with pytest.raises(ValueError, match="require a verification command"):
        no_verifier.dependencies.subagents.delegate(
            "implement", "Create feature.py", ("feature.py",)
        )

    dirty_parent = tmp_path / "dirty"
    dirty_parent.mkdir()
    dirty_root = repository(dirty_parent)
    (dirty_root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    dirty, _ = build_parent(dirty_root, child_factory)
    receipt = delegate_implementation(dirty)

    assert receipt["status"] == "failed"
    assert "unrecorded workspace changes" in receipt["error"]
    assert "patch" not in receipt
    assert "result" not in receipt
    assert calls == []


def test_implement_returns_patch_without_touching_parent(tmp_path):
    root = repository(tmp_path)
    parent, runners = build_parent(
        root,
        lambda spec: implement_client(spec.allowed_write_paths[0]),
        allowed_write_paths=("feature.py",),
    )
    receipt = delegate_implementation(parent)
    manager = parent.dependencies.subagents
    record = parent.run.projection.children.record(receipt["child_id"])
    worktree = manager._worktrees[(parent.run.projection.run_id, receipt["child_id"])]

    assert set(receipt) == CHILD_SUCCESS_FIELDS | {"patch"}
    assert receipt["status"] == "completed"
    assert receipt["patch"]["changed_paths"] == ["feature.py"]
    assert receipt["patch"]["sha256"]
    assert receipt["patch"]["integrated"] is False
    assert not (root / "feature.py").exists()
    assert (
        manager._task_root(parent.run.projection.run_id, record.child_id) / "patch.diff"
    ).is_file()
    assert worktree.path != root
    assert (worktree.path / "feature.py").read_text() == "feature\n"
    assert any(
        runner.calls for runner in runners.runners if runner.root == worktree.path
    )


def test_parent_scope_rejects_implement_before_child_runs(tmp_path):
    calls = []
    parent, _ = build_parent(
        repository(tmp_path),
        lambda _spec: calls.append(True) or implement_client(),
        allowed_write_paths=("safe.py",),
    )
    outcome = run_active(
        parent,
        ToolCall(
            "delegate",
            {
                "role": "implement",
                "task": "Create forbidden.py",
                "allowed_write_paths": ["forbidden.py"],
            },
            "call_scope_rejected",
        ),
    )

    assert outcome.status == "rejected"
    assert "outside allowed scope" in outcome.failure.detail
    assert calls == []


def test_integrate_child_rejects_parent_base_drift(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
    )
    receipt = delegate_implementation(parent)
    (root / "drift.txt").write_text("new base\n")
    git(root, "add", "drift.txt")
    git(root, "commit", "--quiet", "-m", "advance parent")

    with pytest.raises(ValueError, match="parent base changed"):
        parent.dependencies.subagents.integrate_child(receipt["child_id"])

    record = parent.run.projection.children.record(receipt["child_id"])
    assert record.completed().patch.integrated is False
    assert not (root / "feature.py").exists()


def test_failed_integration_verification_does_not_modify_parent(tmp_path):
    root = repository(tmp_path)
    runners = RunnerFactory(fail_integration=True)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
        runner_factory=runners,
    )
    receipt = delegate_implementation(parent)

    with pytest.raises(RuntimeError, match="verification failed"):
        parent.dependencies.subagents.integrate_child(receipt["child_id"])

    record = parent.run.projection.children.record(receipt["child_id"])
    assert record.completed().patch.integrated is False
    assert not (root / "feature.py").exists()
    assert any(runner.fail and runner.calls for runner in runners.runners)


def test_successful_integration_records_parent_workspace_evidence(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
    )
    delegated = run_active(
        parent,
        ToolCall(
            "delegate",
            {
                "role": "implement",
                "task": "Create feature.py",
                "allowed_write_paths": ["feature.py"],
            },
            "call_delegate_before_resume",
        ),
    )
    assert delegated.status == "success"
    receipt = delegated.structured
    outcome = run_active(
        parent,
        ToolCall(
            "integrate_child",
            {"child_id": receipt["child_id"]},
            "call_integrate",
        ),
    )

    assert outcome.status == "success"
    assert outcome.side_effect_state == "changed"
    assert outcome.affected_paths == ("feature.py",)
    assert outcome.structured["status"] == "integrated"
    assert parent.run.evidence.changed_paths == ["feature.py"]
    assert (root / "feature.py").read_text() == "feature\n"
    assert CompletionController(parent).assess("done").allowed
    integration_runner = next(
        runner
        for runner in parent.dependencies.command_runner_factory.runners
        if runner.root.name.startswith("integration-")
    )
    execution = integration_runner.calls[0][1]["execution_context"]
    assert execution.deadline == parent.run.execution_context.deadline


def test_completion_blocks_unintegrated_implementation(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
    )
    receipt = delegate_implementation(parent)
    assessment = CompletionController(parent).assess("done")

    assert assessment.status == "subtasks_incomplete"
    assert receipt["child_id"] in assessment.evidence
    assert not (root / "feature.py").exists()


def test_implement_child_can_complete_without_a_patch(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(root, lambda _spec: FakeModelClient([
        ModelAction.tool("read_file", {"path": "README.md"}),
        ModelAction.final("README already contains demo; no changes needed."),
    ]))
    result = run_active(parent, ToolCall("delegate", {
        "role": "implement", "task": "Ensure README contains demo; keep it if already correct",
        "allowed_write_paths": ["README.md"],
    }))
    assert result.status == "success"
    assert set(result.structured) == CHILD_SUCCESS_FIELDS
    record = parent.run.projection.children.record(result.structured["child_id"])
    assert record.completed().patch is None
    assert parent.dependencies.subagents._worktrees == {}
    assert git(root, "status", "--porcelain") == ""
    replayed = parent.dependencies.run_store.replay(parent.run.projection.run_id)
    assert replayed.children.record(record.child_id).completed().patch is None
    assert CompletionController(parent).assess("Already correct.").allowed


@pytest.mark.parametrize("phase", [
    "before_apply", "applied", "content_conflict", "head_changed", "staged",
    "unrelated_change", "mode_changed", "verification_failed", "recovery_record_failure",
])
def test_real_child_integration_recovers_only_confirmed_application(tmp_path, phase):
    root = repository(tmp_path)
    check = "from pathlib import Path; assert Path('feature.py').read_text() == 'feature\\n'"
    command = shlex.join([sys.executable, "-B", "-c", check])
    factory = lambda _spec: implement_client()
    parent, _ = build_parent(root, factory, verification_command=command,
                             runner_factory=CommandRunner)
    parent.session.set_active_run(parent.run.projection.run_id)
    receipt = delegate_implementation(parent)
    assert receipt["status"] == "completed"
    child_id = receipt["child_id"]
    log = parent.run.run_log
    call = ToolCall("integrate_child", {"child_id": child_id}, "interrupted_integration")
    log.append_tool_calls((call,))
    if phase == "before_apply":
        with (
            patch("pico.subagents.integration.PatchIntegrator._publish", side_effect=SystemExit("crash")),
            pytest.raises(SystemExit),
        ):
            parent.tools.execute_pending_group(log.pending_group_id())
        assert not (root / "feature.py").exists()
    else:
        with (
            patch.object(log, "append_tool_result", side_effect=OSError("crash before result")),
            pytest.raises(OSError),
        ):
            parent.tools.execute_pending_group(log.pending_group_id())
        assert (root / "feature.py").read_text() == "feature\n"
    if phase == "content_conflict":
        (root / "feature.py").write_text("external change\n")
    elif phase == "head_changed":
        git(root, "commit", "--quiet", "--allow-empty", "-m", "new base")
    elif phase == "staged":
        git(root, "add", "feature.py")
    elif phase == "unrelated_change":
        (root / "other.txt").write_text("external change\n")
    elif phase == "mode_changed":
        (root / "feature.py").chmod(0o755)
    elif phase == "verification_failed":
        command = shlex.join([sys.executable, "-B", "-c", "assert False"])
    before_resume = git(root, "status", "--porcelain")
    actions = [ModelAction.final("Implemented.") for _ in range(3)]
    if phase == "before_apply":
        actions.insert(0, ModelAction.tool("integrate_child", {"child_id": child_id}))
    resumed = Pico(
        FakeModelClient(actions), Workspace.build(root),
        config=PicoConfig(mode="auto", verification_command=command),
        session=parent.session.store.load(parent.session.id),
        subagent_model_client_factory=factory if phase == "before_apply" else None,
    )
    if phase == "recovery_record_failure":
        with (
            patch.object(resumed.run.run_log, "append_tool_result", side_effect=OSError("recovery crash")),
            pytest.raises(OSError, match="recovery crash"),
        ):
            resumed.ask("Continue")
        assert resumed.run.run_log.pending_tool_calls()
        resumed = Pico(
            FakeModelClient(actions), Workspace.build(root), config=resumed.config,
            session=parent.session.store.load(parent.session.id),
        )
    outcome = resumed.ask("Continue")
    confirmed = phase in {"before_apply", "applied", "verification_failed", "recovery_record_failure"}
    assert resumed.run.projection.children.record(child_id).completed().patch.integrated is confirmed
    assert outcome.status == ("completed" if confirmed and phase != "verification_failed" else "stopped")
    recovered = [e for e in resumed.run.run_log.events if e.payload.get("recovered_from_interruption")]
    assert len(recovered) == 1
    assert recovered[0].side_effect_state == ("none" if phase == "before_apply" else "partial")
    if phase != "before_apply":
        assert git(root, "status", "--porcelain") == before_resume
        starts = [e for e in resumed.run.run_log.events if e.kind == "tool_started"
                  and e.payload["tool_name"] == "integrate_child"]
        assert len(starts) == 1
    replayed = resumed.dependencies.run_store.replay(outcome.run_id)
    assert replayed.children.record(child_id).completed().patch.integrated is confirmed
    if phase in {"applied", "recovery_record_failure"}:
        assert [v["status"] for v in replayed.evidence.verifications] == ["passed"]
        assert replayed.evidence.partial_workspace_effects()
    elif phase == "verification_failed":
        assert all(v["status"] == "failed" for v in replayed.evidence.verifications)
        assert replayed.evidence.verifications
    parent.dependencies.subagents.cleanup()


def test_explicit_integration_retry_confirms_already_applied_patch(tmp_path):
    root = repository(tmp_path)
    check = "from pathlib import Path; assert Path('feature.py').read_text() == 'feature\\n'"
    parent, _ = build_parent(
        root, lambda _spec: implement_client(),
        verification_command=shlex.join([sys.executable, "-B", "-c", check]),
        runner_factory=CommandRunner,
    )
    receipt = delegate_implementation(parent)
    tool = parent.tools.registry["integrate_child"]
    runner = tool["run"]

    def fail_after_apply(context, args):
        runner(context, args)
        raise RuntimeError("injected failure after applying patch")

    with patch.dict(tool, run=fail_after_apply):
        failed = run_active(parent, ToolCall("integrate_child", {
            "child_id": receipt["child_id"],
        }, "first"))
    assert failed.side_effect_state == "partial"
    with patch("pico.subagents.integration.PatchIntegrator._publish", side_effect=AssertionError("must not reapply")):
        retried = run_active(parent, ToolCall("integrate_child", {
            "child_id": receipt["child_id"],
        }, "retry"))
    assert retried.status == "success"
    assert retried.side_effect_state == "none"
    assert retried.structured["path_transitions"] == []
    assert parent.run.projection.children.record(receipt["child_id"]).completed().patch.integrated
    assert CompletionController(parent).assess("done").allowed
    assert [v["status"] for v in parent.run.evidence.verifications] == ["passed"]


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_child_delivers_recorded_gitignored_changes(tmp_path, mixed, interrupted):
    root = repository(tmp_path)
    (root / ".gitignore").write_text(".pico/\nignored.txt\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "--quiet", "-m", "ignore local output")
    paths = ["ignored.txt", "normal.txt"] if mixed else ["ignored.txt"]
    check = f"from pathlib import Path; assert all(Path(p).read_text() == 'required' for p in {paths!r})"
    actions = [ModelAction.tool("write_file", {"path": path, "content": "required"}) for path in paths]
    actions.append(ModelAction.final("Files created."))
    parent, _ = build_parent(
        root, lambda _spec: FakeModelClient(actions),
        verification_command=shlex.join([sys.executable, "-B", "-c", check]),
        runner_factory=CommandRunner,
    )
    parent.session.set_active_run(parent.run.projection.run_id)
    delegated = run_active(parent, ToolCall("delegate", {
        "role": "implement", "task": "Create the required files", "allowed_write_paths": paths,
    }))
    assert delegated.status == "success"
    receipt = delegated.structured
    assert receipt["patch"]["changed_paths"] == paths
    record = parent.run.projection.children.record(receipt["child_id"])
    child = parent.dependencies.subagents._child_projection(parent.run.projection.run_id, record)
    assert child.evidence.changed_paths == paths
    call = ToolCall("integrate_child", {"child_id": record.child_id}, "integrate")
    if interrupted:
        parent.run.run_log.append_tool_calls((call,))
        with (
            patch.object(parent.run.run_log, "append_tool_result", side_effect=OSError("crash")),
            pytest.raises(OSError),
        ):
            parent.tools.execute_pending_group(parent.run.run_log.pending_group_id())
        parent = Pico(
            FakeModelClient([ModelAction.final("Files delivered.")]), Workspace.build(root),
            config=parent.config, session=parent.session.store.load(parent.session.id),
        )
        assert parent.ask("Continue").status == "completed"
    else:
        assert run_active(parent, call).status == "success"
        assert CompletionController(parent).assess("Files delivered.").allowed
    assert parent.run.evidence.changed_paths == paths
    assert parent.run.projection.children.record(record.child_id).completed().patch.integrated
    assert all((root / path).read_text() == "required" for path in paths)
    assert git(root, "ls-files", "--", "ignored.txt") == ""


def test_child_packaging_failure_retains_worktree_and_reports_its_path(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(root, lambda _spec: implement_client())
    retained = []

    def fail_packaging(handle, destination, changed_paths):
        retained.append(handle)
        raise OSError("cannot write delivery patch")

    with patch.object(GitWorktree, "write_patch", fail_packaging):
        receipt = delegate_implementation(parent)
    handle = retained[0]
    try:
        parent.dependencies.subagents.cleanup()
        assert receipt["status"] == "failed"
        assert str(handle.path) in receipt["error"]
        assert (handle.path / "feature.py").read_text() == "feature\n"
        assert not (root / "feature.py").exists()
    finally:
        handle.cleanup()


def test_two_disjoint_child_patches_can_be_delivered_sequentially(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(root, lambda spec: implement_client(spec.allowed_write_paths[0]))
    first = delegate_implementation(parent, "first.py")
    second = delegate_implementation(parent, "second.py")
    for receipt in (first, second):
        result = run_active(parent, ToolCall("integrate_child", {"child_id": receipt["child_id"]}))
        assert result.status == "success", result.content
    assert parent.run.evidence.changed_paths == ["first.py", "second.py"]
    assert CompletionController(parent).assess("done").allowed


@pytest.mark.parametrize("phase", ["normal", "conflict", "before_publish", "after_publish", "later_edit", "sequential_delegation"])
def test_combined_child_delivery_and_recovery_use_transaction_state(tmp_path, phase):
    root = repository(tmp_path)
    source = "def a():\n    return 0\n" + "\n" * 20 + "def b():\n    return 0\n"
    (root / "common.py").write_text(source)
    git(root, "add", "common.py")
    git(root, "commit", "--quiet", "-m", "shared source")

    def child_client(spec):
        name = "a" if spec.task == "first" or phase == "conflict" else "b"
        value = 1 if spec.task == "first" else 2
        return FakeModelClient([
            ModelAction.tool("read_file", {"path": "common.py"}),
            ModelAction.tool("edit_file", {
                "path": "common.py", "old_text": f"def {name}():\n    return 0",
                "new_text": f"def {name}():\n    return {value}", "expected_revision": file_revision(root / "common.py"),
            }), ModelAction.final("done"),
        ])

    check = (
        "from pathlib import Path; scope={}; exec(Path('common.py').read_text(), scope); "
        "assert scope['a']() >= 0 and scope['b']() >= 0"
    )
    parent, _ = build_parent(root, child_client, runner_factory=CommandRunner,
        verification_command=shlex.join([sys.executable, "-B", "-c", check]))
    parent.session.set_active_run(parent.run.projection.run_id)
    receipts = []
    for task in ("first", "second"):
        result = run_active(parent, ToolCall("delegate", {
            "role": "implement", "task": task, "allowed_write_paths": ["common.py"],
        }))
        assert result.status == "success", result.content
        receipts.append(result.structured)
        if phase == "sequential_delegation" and task == "first":
            assert run_active(parent, ToolCall("integrate_child", {"child_id": receipts[0]["child_id"]})).status == "success"
    if phase != "sequential_delegation":
        assert run_active(parent, ToolCall("integrate_child", {"child_id": receipts[0]["child_id"]})).status == "success"
    before_second = (root / "common.py").read_text()
    second = ToolCall("integrate_child", {"child_id": receipts[1]["child_id"]}, "second")
    if phase == "before_publish":
        with (
            patch("pico.subagents.integration.PatchIntegrator._publish", side_effect=SystemExit("crash")),
            pytest.raises(SystemExit),
        ):
            run_active(parent, second)
    elif phase == "after_publish":
        with (
            patch.object(parent.run.run_log, "append_tool_result", side_effect=OSError("crash")),
            pytest.raises(OSError),
        ):
            run_active(parent, second)
    elif phase == "later_edit":
        tool = parent.tools.registry["integrate_child"]
        runner = tool["run"]

        def fail_after_apply(context, args):
            runner(context, args)
            raise RuntimeError("interrupted after application")

        with patch.dict(tool, run=fail_after_apply):
            assert run_active(parent, second).side_effect_state == "partial"
        edited = run_active(parent, ToolCall("edit_file", {
            "path": "common.py", "old_text": "def b():\n    return 2",
            "new_text": "def b():\n    return 3", "expected_revision": file_revision(root / "common.py"),
        }))
        assert edited.status == "success"
        assert run_active(parent, ToolCall("integrate_child", second.args, "retry")).side_effect_state == "none"
    else:
        result = run_active(parent, second)
        if phase == "conflict":
            assert result.status == "error"
            assert (root / "common.py").read_text() == before_second
            assert CompletionController(parent).assess("done").status == "subtasks_incomplete"
            parent.dependencies.subagents.cleanup()
            return
        assert result.status == "success", result.content
    if phase in {"before_publish", "after_publish"}:
        actions = [ModelAction.final("done")]
        if phase == "before_publish":
            actions.insert(0, ModelAction.tool("integrate_child", second.args))
        parent = Pico(FakeModelClient(actions), Workspace.build(root), config=parent.config,
            session=parent.session.store.load(parent.session.id),
            subagent_model_client_factory=child_client if phase == "before_publish" else None)
        assert parent.ask("Continue").status == "completed"
    else:
        assert CompletionController(parent).assess("done").allowed
    content = (root / "common.py").read_text()
    assert "def a():\n    return 1" in content
    assert f"def b():\n    return {3 if phase == 'later_edit' else 2}" in content
    assert parent.run.projection.children.record(receipts[1]["child_id"]).completed().patch.integrated
    assert git(root, "diff", "--cached", "--name-only") == ""
