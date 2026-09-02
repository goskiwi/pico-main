import subprocess
from pathlib import Path

import pytest

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    WorkspaceContext,
)
from pico.command_runner import CommandResult
from pico.completion_controller import CompletionController
from pico.contracts import ToolCall
from pico.execution import ExecutionContext
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract

CHILD_RECEIPT_FIELDS = {
    "child_id",
    "role",
    "status",
    "child_run_id",
    "result",
    "base_sha",
    "changed_paths",
    "patch_sha256",
    "error",
    "integrated",
}


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
        parent.session.data["id"],
        parent.dependencies.run_store,
    )
    first = run_log.append_user(
        TaskContract(
            goal="Parent task",
            allows_workspace_mutation=allows_workspace_mutation,
            verify_changes=False,
            allowed_write_paths=allowed_write_paths,
        )
    )
    parent.run.projection = RunProjection().apply_event(first)
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
    parent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(root),
        SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(
            mode="auto",
            verification_command=verification_command,
            allowed_write_paths=allowed_write_paths,
        ),
        command_runner_factory=runner_factory,
        subagent_model_client_factory=child_factory,
    )
    activate_parent(
        parent,
        allows_workspace_mutation=allows_workspace_mutation,
        allowed_write_paths=allowed_write_paths,
    )
    return parent, runner_factory


def run_active(parent, call):
    parent.apply_run_event(parent.run.run_log.append_tool_call(call))
    return parent.tools.execute_pending(call.call_id)


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
    return parent.dependencies.subagents.delegate(
        "implement",
        f"Create {path}",
        (path,),
    )


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
    assert all(set(receipt) == CHILD_RECEIPT_FIELDS for receipt in receipts)
    assert all(receipt["status"] == "completed" for receipt in receipts)
    assert receipts[0]["child_id"] != receipts[1]["child_id"]
    assert receipts[0]["child_run_id"] != receipts[1]["child_run_id"]

    manager = parent.dependencies.subagents
    for receipt, client in zip(receipts, clients):
        record = manager._record(parent.run.projection.run_id, receipt["child_id"])
        projection = manager._child_projection(parent.run.projection.run_id, record)
        child_tools = set(client.action_tool_surfaces[0])
        assert projection.task.contract.allows_workspace_mutation is False
        assert {"delegate", "integrate_child", "write_file", "edit_file"}.isdisjoint(
            child_tools
        )
        assert all(0 < timeout <= 60 for timeout in client.request_timeouts)


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
    assert "clean working tree" in receipt["error"]
    assert receipt["changed_paths"] == []
    assert receipt["patch_sha256"] == ""
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
    record = manager._record(parent.run.projection.run_id, receipt["child_id"])
    worktree = manager._worktrees[
        (parent.run.projection.run_id, receipt["child_id"])
    ]

    assert set(receipt) == CHILD_RECEIPT_FIELDS
    assert receipt["status"] == "completed"
    assert receipt["changed_paths"] == ["feature.py"]
    assert receipt["patch_sha256"] and receipt["integrated"] is False
    assert not (root / "feature.py").exists()
    assert Path(record.patch_path).is_file()
    assert worktree.path != root
    assert (worktree.path / "feature.py").read_text() == "feature\n"
    assert any(runner.calls for runner in runners.runners if runner.root == worktree.path)


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

    record = parent.dependencies.subagents._record(
        parent.run.projection.run_id, receipt["child_id"]
    )
    assert record.integrated is False
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

    record = parent.dependencies.subagents._record(
        parent.run.projection.run_id, receipt["child_id"]
    )
    assert record.integrated is False
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
    assert receipt["child_id"] in assessment.instruction
    assert not (root / "feature.py").exists()
