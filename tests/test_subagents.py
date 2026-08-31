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
from pico.contracts import ToolCall, ToolOutcome
from pico.execution import ExecutionContext
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.subagents import ChildSpec, SubagentRunner
from pico.subagents.tools import DelegateArgs, IntegrateChildArgs
from pico.task_state import TaskContract
from pico.tools import function_schema

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
    task_kind="modify",
    requires_workspace_change=False,
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
            task_kind=task_kind,
            requires_workspace_change=requires_workspace_change,
            requires_verification=False,
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
    requires_workspace_change=False,
    task_kind="modify",
    runner_factory=None,
):
    runner_factory = runner_factory or RunnerFactory()
    parent = Pico(
        FakeModelClient([]),
        WorkspaceContext.build(root),
        SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command=verification_command,
            allowed_write_paths=allowed_write_paths,
        ),
        command_runner_factory=runner_factory,
        subagent_model_client_factory=child_factory,
    )
    activate_parent(
        parent,
        task_kind=task_kind,
        requires_workspace_change=requires_workspace_change,
        allowed_write_paths=allowed_write_paths,
    )
    return parent, runner_factory


def run_active(parent, call):
    parent.apply_run_event(parent.run.run_log.append_tool_call(call))
    return parent.tools.execute(call)


def reopen_parent(parent, child_factory, *, outputs=()):
    parent.session.set_active_run(parent.run.projection.run_id)
    return Pico(
        FakeModelClient(list(outputs)),
        WorkspaceContext.build(parent.workspace.root),
        parent.session.store,
        session=parent.session.store.load(parent.session.data["id"]),
        config=parent.config,
        command_runner_factory=parent.dependencies.command_runner_factory,
        subagent_model_client_factory=child_factory,
    )


def explore_client(answer="Findings: README.md contains demo."):
    return FakeModelClient(
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


def test_child_contract_and_tool_schema_are_single_child_actions(tmp_path):
    parent, _ = build_parent(repository(tmp_path), lambda _spec: explore_client())
    delegate_schema = function_schema(DelegateArgs)
    integrate_schema = function_schema(IntegrateChildArgs)

    assert set(delegate_schema["properties"]) == {
        "role",
        "task",
        "allowed_write_paths",
    }
    assert delegate_schema["properties"]["role"]["enum"] == [
        "explore",
        "implement",
    ]
    assert set(integrate_schema["properties"]) == {"child_id"}
    assert {"delegate", "integrate_child"} <= {
        tool["name"] for tool in parent.tools.action_schemas
    }
    with pytest.raises(ValueError, match="cannot declare write paths"):
        ChildSpec(
            role="explore",
            task="Inspect",
            allowed_write_paths=("README.md",),
        )
    with pytest.raises(ValueError, match="require allowed_write_paths"):
        ChildSpec(role="implement", task="Change")


def test_explore_creates_distinct_read_only_non_recursive_children(tmp_path):
    clients = []

    def child_factory(spec):
        assert spec.role == "explore"
        client = explore_client(f"handoff {len(clients) + 1}")
        clients.append(client)
        return client

    parent, _ = build_parent(
        repository(tmp_path),
        child_factory,
        task_kind="read_only",
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
        assert projection.task.contract.task_kind == "read_only"
        assert {"delegate", "integrate_child", "write_file", "edit_file"}.isdisjoint(
            child_tools
        )


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


def test_integrate_child_requires_explicit_approval(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
    )
    receipt = delegate_implementation(parent)
    parent.config = PicoConfig.build(parent.config, approval_policy="deny")
    outcome = run_active(
        parent,
        ToolCall(
            "integrate_child",
            {"child_id": receipt["child_id"]},
            "call_denied_integration",
        ),
    )
    record = parent.dependencies.subagents._record(
        parent.run.projection.run_id, receipt["child_id"]
    )

    assert parent.tools.registry["integrate_child"]["risky"] is True
    assert outcome.status == "rejected"
    assert outcome.failure.code == "approval_denied"
    assert record.integrated is False
    assert not (root / "feature.py").exists()


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


def test_verifier_cannot_add_a_path_outside_the_child_receipt(tmp_path):
    root = repository(tmp_path)
    runners = MutatingRunnerFactory(
        lambda workspace: (workspace / "verifier-extra.txt").write_text(
            "unexpected\n",
            encoding="utf-8",
        )
    )
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
        runner_factory=runners,
    )
    receipt = delegate_implementation(parent)

    with pytest.raises(RuntimeError, match="changed additional workspace state"):
        parent.dependencies.subagents.integrate_child(receipt["child_id"])

    record = parent.dependencies.subagents._record(
        parent.run.projection.run_id, receipt["child_id"]
    )
    assert record.integrated is False
    assert not (root / "feature.py").exists()
    assert not (root / "verifier-extra.txt").exists()
    assert git(root, "status", "--porcelain") == ""


def test_verifier_cannot_modify_the_immutable_child_patch(tmp_path):
    root = repository(tmp_path)
    runners = MutatingRunnerFactory(
        lambda workspace: (workspace / "feature.py").write_text(
            "tampered by verifier\n",
            encoding="utf-8",
        )
    )
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
        runner_factory=runners,
    )
    receipt = delegate_implementation(parent)

    with pytest.raises(RuntimeError, match="changed Runtime-tracked file contents"):
        parent.dependencies.subagents.integrate_child(receipt["child_id"])

    record = parent.dependencies.subagents._record(
        parent.run.projection.run_id, receipt["child_id"]
    )
    assert record.integrated is False
    assert not (root / "feature.py").exists()
    assert git(root, "status", "--porcelain") == ""


def test_successful_integration_records_parent_workspace_evidence(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
        requires_workspace_change=True,
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


def test_completion_blocks_unintegrated_implementation(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
        requires_workspace_change=True,
    )
    receipt = delegate_implementation(parent)
    assessment = CompletionController(parent).assess("done")

    assert assessment.status == "subtasks_incomplete"
    assert receipt["child_id"] in assessment.instruction
    assert not (root / "feature.py").exists()


def test_failed_child_is_a_structured_delegate_error(tmp_path):
    parent, _ = build_parent(
        repository(tmp_path),
        lambda _spec: FakeModelClient(
            [ModelAction.invalid("invalid child action") for _ in range(8)]
        ),
        task_kind="read_only",
    )
    outcome = run_active(
        parent,
        ToolCall(
            "delegate",
            {
                "role": "explore",
                "task": "Fail in a controlled way",
                "allowed_write_paths": [],
            },
            "call_failed_child",
        ),
    )

    assert outcome.status == "error"
    assert outcome.execution_state == "completed"
    assert outcome.side_effect_state == "none"
    assert outcome.failure.code == "child_failed"
    assert set(outcome.structured) == CHILD_RECEIPT_FIELDS
    assert outcome.structured["child_id"].startswith("child_")
    assert outcome.structured["status"] == "failed"
    assert outcome.structured["error"]
    assert outcome.structured["integrated"] is False


def test_new_runner_recovers_unintegrated_child_and_later_integration(tmp_path):
    root = repository(tmp_path)
    parent, _ = build_parent(
        root,
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
        requires_workspace_change=True,
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

    resumed = reopen_parent(parent, lambda _spec: implement_client())
    runner = resumed.dependencies.subagents
    issue = runner.completion_issue()
    recovered = runner._record(resumed.run.projection.run_id, receipt["child_id"])

    assert receipt["child_id"] in issue
    assert recovered.status == "completed"
    assert recovered.integrated is False
    assert recovered.spec.task == "Create feature.py"
    assert recovered.spec.allowed_write_paths == ("feature.py",)
    assert recovered.patch_path == str(
        resumed.dependencies.run_store.run_dir(resumed.run.projection.run_id)
        / "subagents"
        / receipt["child_id"]
        / "patch.diff"
    )

    RunLifecycle(resumed).initialize(
        "Continue",
        task_intent="modify",
    )
    outcome = run_active(
        resumed,
        ToolCall(
            "integrate_child",
            {"child_id": receipt["child_id"]},
            "call_integrate_after_resume",
        ),
    )
    assert outcome.status == "success"
    assert (root / "feature.py").read_text() == "feature\n"
    resumed.run.execution_context = None

    reopened = reopen_parent(resumed, lambda _spec: implement_client())
    restored = reopened.dependencies.subagents._record(
        reopened.run.projection.run_id,
        receipt["child_id"],
    )
    assert restored.integrated is True
    assert reopened.dependencies.subagents.completion_issue() == ""


def test_malformed_persisted_delegate_receipt_fails_closed(tmp_path):
    parent, _ = build_parent(
        repository(tmp_path),
        lambda _spec: implement_client(),
        allowed_write_paths=("feature.py",),
    )
    call = ToolCall(
        "delegate",
        {
            "role": "implement",
            "task": "Create feature.py",
            "allowed_write_paths": ["feature.py"],
        },
        "call_malformed_receipt",
    )
    parent.apply_run_event(parent.run.run_log.append_tool_call(call))
    parent.apply_run_event(
        parent.run.run_log.append_tool_started(
            call,
            risky=False,
            effect_scope="none",
            potential_effects=[],
        )
    )
    parent.apply_run_event(
        parent.run.run_log.append_tool_result(
            ToolOutcome(
                tool_call_id=call.call_id,
                tool_name=call.name,
                status="success",
                execution_state="completed",
                side_effect_state="none",
                content="malformed",
                structured={"child_id": "child_012345abcdef"},
            )
        )
    )

    fresh_runner = SubagentRunner(parent, lambda _spec: implement_client())
    with pytest.raises(ValueError, match="invalid persisted delegate receipt"):
        fresh_runner.completion_issue()
