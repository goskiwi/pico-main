import json
import os
import subprocess
import threading
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
from pico.mutations import content_revision
from pico.run_store import RunStore
from pico.sandbox import DockerSandbox, SandboxResult
from pico.subagents import SubtaskSpec
from pico.subagents.tools import DelegateTasksArgs
from pico.subagents.worktree import GitWorktree, GitWorktreeError
from pico.tools import function_schema


def git(root, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def repository(tmp_path, *, ignore_pico=True):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "pico@example.test")
    git(root, "config", "user.name", "Pico Test")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    tracked = ["README.md"]
    if ignore_pico:
        (root / ".gitignore").write_text(".pico/\n", encoding="utf-8")
        tracked.append(".gitignore")
    git(root, "add", *tracked)
    git(root, "commit", "-m", "base")
    return root


class PassingSandbox:
    def __init__(self, root, *, side_effect=None):
        self.root = Path(root)
        self.side_effect = side_effect

    def run(self, *_args, **_kwargs):
        if self.side_effect is not None:
            self.side_effect(self.root)
        return SandboxResult(returncode=0, stdout="2 passed\n")


def build_parent(
    root,
    child_factory,
    *,
    sandbox_factory=None,
    parent_outputs=(),
    verification_command="python -m pytest -q",
):
    sandbox_factory = sandbox_factory or (lambda child_root: PassingSandbox(child_root))
    return Pico(
        model_client=FakeModelClient(list(parent_outputs)),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command=verification_command,
        ),
        sandbox_factory=sandbox_factory,
        subagent_model_client_factory=child_factory,
    )


class BarrierClient(FakeModelClient):
    def __init__(self, barrier, label):
        super().__init__([ModelAction.final(label)])
        self.barrier = barrier

    def complete_action(self, *args, **kwargs):
        self.barrier.wait(timeout=3)
        return super().complete_action(*args, **kwargs)


def test_worktree_create_failure_removes_temporary_container(tmp_path, monkeypatch):
    container = tmp_path / "failed-worktree"

    def make_container(*_args, **_kwargs):
        container.mkdir()
        return str(container)

    def fail_git(*_args, **_kwargs):
        raise GitWorktreeError("planned create failure")

    monkeypatch.setattr(
        "pico.subagents.worktree.tempfile.mkdtemp",
        make_container,
    )
    monkeypatch.setattr("pico.subagents.worktree._git", fail_git)
    handle = GitWorktree(tmp_path, "base", "child")

    with pytest.raises(GitWorktreeError, match="planned create failure"):
        handle.create()

    assert not container.exists()
    assert handle.container_root is None
    assert handle.path is None


def test_parent_tool_runs_parallel_children_with_isolated_runtime_state(tmp_path):
    root = repository(tmp_path)
    barrier = threading.Barrier(2)
    clients = []

    def child_factory(spec):
        client = BarrierClient(barrier, f"result-{spec.task_id}")
        clients.append(client)
        return client

    tasks = [
        {
            "task_id": "explore-auth",
            "kind": "explore",
            "prompt": "inspect auth",
            "depends_on": [],
            "allowed_write_paths": [],
            "max_tool_executions": 4,
        },
        {
            "task_id": "explore-cache",
            "kind": "explore",
            "prompt": "inspect cache",
            "depends_on": [],
            "allowed_write_paths": [],
            "max_tool_executions": 4,
        },
    ]
    parent = build_parent(
        root,
        child_factory,
        parent_outputs=[
            ModelAction.tool("delegate_tasks", {"tasks": tasks}, call_id="delegate"),
            ModelAction.final("Parent synthesized both results."),
        ],
    )

    answer = parent.ask("Inspect auth and cache in parallel")

    assert answer == "Parent synthesized both results."
    tool_result = parent.model_client.recorded_action_results[0][1]
    payload = json.loads(tool_result)
    receipts = payload["structured"]["tasks"]
    assert {item["status"] for item in receipts} == {"completed"}
    assert len({item["child_run_id"] for item in receipts}) == 2
    assert all("delegate_tasks" not in client.action_tool_surfaces[0] for client in clients)
    assert all("exact repository paths, line ranges" in client.prompts[0] for client in clients)
    assert "delegate_tasks" in parent.model_client.action_tool_surfaces[0]


def test_delegate_tool_schema_is_strict_at_nested_task_level():
    schema = function_schema(DelegateTasksArgs)
    task_schema = schema["$defs"]["SubtaskSpec"]

    assert set(schema["required"]) == set(schema["properties"])
    assert set(task_schema["required"]) == set(task_schema["properties"])
    assert task_schema["additionalProperties"] is False
    assert task_schema["properties"]["max_tool_executions"]["maximum"] == 12

    with pytest.raises(ValueError, match="less than or equal to 12"):
        SubtaskSpec(
            task_id="explore-too-long",
            kind="explore",
            prompt="inspect",
            max_tool_executions=13,
        )


def test_one_delegation_cannot_mix_explore_and_implement_tasks(tmp_path):
    root = repository(tmp_path)
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient([ModelAction.final("unused")]),
    )

    with pytest.raises(ValueError, match="only explore tasks or only implement tasks"):
        parent.dependencies.subagents.delegate(
            (
                SubtaskSpec(
                    task_id="explore-first",
                    kind="explore",
                    prompt="inspect",
                ),
                SubtaskSpec(
                    task_id="implement-after",
                    kind="implement",
                    prompt="implement",
                    depends_on=("explore-first",),
                    allowed_write_paths=("feature.py",),
                ),
            )
        )


def test_explore_handoff_feeds_separate_implement_delegation(tmp_path):
    root = repository(tmp_path)
    clients = {}

    def child_factory(spec):
        if spec.kind == "explore":
            client = FakeModelClient([ModelAction.final("change feature.py line 1")])
        else:
            client = FakeModelClient(
                [
                    ModelAction.tool(
                        "write_file",
                        {
                            "path": "feature.py",
                            "content": "implemented\n",
                            "expected_revision": "absent",
                        },
                    ),
                    ModelAction.final("implemented handoff"),
                ]
            )
        clients[spec.task_id] = client
        return client

    parent = build_parent(root, child_factory)
    explore = SubtaskSpec(
        task_id="explore-feature",
        kind="explore",
        prompt="inspect feature",
    )
    implement = SubtaskSpec(
        task_id="implement-feature",
        kind="implement",
        prompt="implement the synthesized specification",
        depends_on=("explore-feature",),
        allowed_write_paths=("feature.py",),
    )

    first = parent.dependencies.subagents.delegate((explore,))
    second = parent.dependencies.subagents.delegate((implement,))

    assert first["tasks"][0]["status"] == "completed"
    assert second["tasks"][0]["status"] == "completed"
    assert "change feature.py line 1" in clients["implement-feature"].prompts[0]
    assert "make the first mutation after at most three" in clients[
        "implement-feature"
    ].prompts[0]
    implement_tools = clients["implement-feature"].action_tool_surfaces[0]
    assert "search" not in implement_tools
    assert "list_files" not in implement_tools


def test_dag_orders_dependencies_and_blocks_failed_branch_only(tmp_path):
    root = repository(tmp_path)
    started = []

    class RecordingClient(FakeModelClient):
        def __init__(self, task_id, fail=False):
            super().__init__([ModelAction.final(f"done-{task_id}")])
            self.task_id = task_id
            self.fail = fail

        def complete_action(self, *args, **kwargs):
            started.append(self.task_id)
            if self.fail:
                raise RuntimeError("planned child failure")
            return super().complete_action(*args, **kwargs)

    parent = build_parent(
        root,
        lambda spec: RecordingClient(spec.task_id, spec.task_id == "explore-bad"),
    )
    result = parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="explore-bad",
                kind="explore",
                prompt="fail",
            ),
            SubtaskSpec(
                task_id="explore-good",
                kind="explore",
                prompt="succeed",
            ),
            SubtaskSpec(
                task_id="explore-dependent",
                kind="explore",
                prompt="blocked",
                depends_on=("explore-bad",),
            ),
            SubtaskSpec(
                task_id="explore-after-good",
                kind="explore",
                prompt="run later",
                depends_on=("explore-good",),
            ),
        )
    )
    by_id = {item["task_id"]: item for item in result["tasks"]}

    assert by_id["explore-bad"]["status"] == "failed"
    assert by_id["explore-dependent"]["status"] == "blocked"
    assert by_id["explore-good"]["status"] == "completed"
    assert by_id["explore-after-good"]["status"] == "completed"
    assert started.index("explore-after-good") > started.index("explore-good")
    assert "explore-dependent" not in started


def test_dag_rejects_cycles_unknown_dependencies_and_unordered_write_overlap(
    tmp_path,
):
    root = repository(tmp_path)
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient([ModelAction.final("unused")]),
    )

    with pytest.raises(ValueError, match="cycle"):
        parent.dependencies.subagents.delegate(
            (
                SubtaskSpec(
                    task_id="explore-a",
                    kind="explore",
                    prompt="a",
                    depends_on=("explore-b",),
                ),
                SubtaskSpec(
                    task_id="explore-b",
                    kind="explore",
                    prompt="b",
                    depends_on=("explore-a",),
                ),
            )
        )

    with pytest.raises(ValueError, match="unknown dependencies"):
        parent.dependencies.subagents.delegate(
            (
                SubtaskSpec(
                    task_id="explore-c",
                    kind="explore",
                    prompt="c",
                    depends_on=("missing",),
                ),
            )
        )

    with pytest.raises(ValueError, match="overlapping write paths"):
        parent.dependencies.subagents.delegate(
            (
                SubtaskSpec(
                    task_id="implement-a",
                    kind="implement",
                    prompt="a",
                    allowed_write_paths=("shared.py",),
                ),
                SubtaskSpec(
                    task_id="implement-b",
                    kind="implement",
                    prompt="b",
                    allowed_write_paths=("shared.py",),
                ),
            )
        )


def test_implement_delegation_requires_verification_command(tmp_path):
    root = repository(tmp_path)
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient([ModelAction.final("unused")]),
        verification_command="",
    )

    with pytest.raises(ValueError, match="require a verification command"):
        parent.dependencies.subagents.delegate(
            (
                SubtaskSpec(
                    task_id="implement-without-verifier",
                    kind="implement",
                    prompt="create feature.py",
                    allowed_write_paths=("feature.py",),
                ),
            )
        )

    assert parent.dependencies.subagents._records("manual") == {}
    assert parent.dependencies.subagents._worktrees == {}


def test_implementation_worktrees_are_isolated_then_verified_and_applied(tmp_path):
    root = repository(tmp_path)
    clients = []

    def child_factory(spec):
        target = spec.allowed_write_paths[0]
        client = FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": target,
                        "content": f"{spec.task_id}\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final(f"implemented {target}"),
            ]
        )
        clients.append(client)
        return client

    parent = build_parent(root, child_factory)
    result = parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-auth",
                kind="implement",
                prompt="implement auth",
                allowed_write_paths=("auth.py",),
            ),
            SubtaskSpec(
                task_id="implement-cache",
                kind="implement",
                prompt="implement cache",
                allowed_write_paths=("cache.py",),
            ),
        )
    )

    assert {item["status"] for item in result["tasks"]} == {"completed"}
    records = parent.dependencies.subagents._records("manual")
    assert {record.base_sha for record in records.values()} == {
        git(root, "rev-parse", "HEAD")
    }
    assert not (root / "auth.py").exists()
    assert not (root / "cache.py").exists()
    worktrees = [
        parent.dependencies.subagents._worktrees[("manual", task_id)].path
        for task_id in ("implement-auth", "implement-cache")
    ]
    assert worktrees[0] != worktrees[1]
    assert (worktrees[0] / "auth.py").exists()
    assert not (worktrees[0] / "cache.py").exists()
    assert (worktrees[1] / "cache.py").exists()
    assert not (worktrees[1] / "auth.py").exists()
    assert all("Do not run git commands" in client.prompts[0] for client in clients)
    assert all(
        "Configured verification command:\npython -m pytest -q" in client.prompts[0]
        for client in clients
    )

    applied = parent.dependencies.subagents.integration.apply(
        ("implement-auth", "implement-cache")
    )

    assert applied["status"] == "applied"
    assert applied["verification"]["exit_code"] == 0
    assert applied["verification"]["status"] == "passed"
    assert applied["verification"]["finished_workspace_mutation_sequence"] == 0
    assert (root / "auth.py").read_text() == "implement-auth\n"
    assert (root / "cache.py").read_text() == "implement-cache\n"


def test_parallel_implementation_patches_must_be_applied_together(tmp_path):
    root = repository(tmp_path)

    def child_factory(spec):
        target = spec.allowed_write_paths[0]
        return FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": target,
                        "content": f"{spec.task_id}\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("implemented"),
            ]
    )

    parent = build_parent(root, child_factory)
    parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-a",
                kind="implement",
                prompt="create a.py",
                allowed_write_paths=("a.py",),
            ),
            SubtaskSpec(
                task_id="implement-b",
                kind="implement",
                prompt="create b.py",
                allowed_write_paths=("b.py",),
            ),
        )
    )

    with pytest.raises(ValueError, match="apply all completed patches together"):
        parent.dependencies.subagents.integration.apply(("implement-a",))

    assert not (root / "a.py").exists()
    assert not (root / "b.py").exists()

    result = parent.dependencies.subagents.integration.apply(
        ("implement-a", "implement-b")
    )

    assert result["status"] == "applied"
    assert (root / "a.py").is_file()
    assert (root / "b.py").is_file()


def test_write_scope_is_enforced_before_execution(tmp_path):
    root = repository(tmp_path)

    def child_factory(_spec):
        return FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": "forbidden.py",
                        "content": "bad\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("done"),
            ]
        )

    parent = build_parent(root, child_factory)
    result = parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-safe",
                kind="implement",
                prompt="write only safe.py",
                allowed_write_paths=("safe.py",),
            ),
        )
    )
    receipt = result["tasks"][0]
    record = parent.dependencies.subagents._records("manual")["implement-safe"]

    assert receipt["status"] == "failed"
    assert not Path(record.patch_path or root / "missing").exists()
    assert ("manual", "implement-safe") not in parent.dependencies.subagents._worktrees
    run_store = RunStore(
        parent.dependencies.run_store.run_dir("manual")
        / "subagents"
        / record.spec.task_id
        / "runs"
    )
    entries = run_store.read_events(record.child_run_id)
    rejection = next(
        entry
        for entry in entries
        if entry.kind == "tool_result"
        and entry.payload["outcome"]["status"] == "rejected"
    )
    assert rejection.payload["outcome"]["failure"]["detail"].startswith(
        "write path outside allowed scope"
    )


def test_post_execution_diff_detects_an_out_of_scope_side_effect(tmp_path):
    root = repository(tmp_path)

    def sandbox_factory(child_root):
        child_root = Path(child_root)

        def side_effect(workspace):
            if workspace != root:
                (workspace / "forbidden.py").write_text("escaped\n", encoding="utf-8")

        return PassingSandbox(child_root, side_effect=side_effect)

    def child_factory(_spec):
        return FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": "safe.py",
                        "content": "safe\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("done"),
            ]
        )

    parent = build_parent(root, child_factory, sandbox_factory=sandbox_factory)
    receipt = parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-escape",
                kind="implement",
                prompt="write safe.py",
                allowed_write_paths=("safe.py",),
            ),
        )
    )["tasks"][0]

    assert receipt["status"] == "failed"
    assert receipt["changed_paths"] == ["forbidden.py", "safe.py"]
    assert "write scope violation after execution" in receipt["error"]
    assert ("manual", "implement-escape") not in parent.dependencies.subagents._worktrees


def test_completed_task_id_is_reused_without_another_model_call(tmp_path):
    root = repository(tmp_path)
    calls = 0

    def child_factory(_spec):
        nonlocal calls
        calls += 1
        return FakeModelClient([ModelAction.final("done")])

    parent = build_parent(root, child_factory)
    spec = SubtaskSpec(
        task_id="explore-reuse",
        kind="explore",
        prompt="inspect once",
    )

    parent.dependencies.subagents.delegate((spec,))
    second = parent.dependencies.subagents.delegate((spec,))

    assert calls == 1
    assert second["tasks"][0]["reused"] is True
    record = parent.dependencies.subagents._records("manual")["explore-reuse"]
    assert record.status == "completed"
    assert not hasattr(record, "result")
    assert not hasattr(record, "worktree")


def test_ordered_implement_dependency_receives_prior_patch_and_integrates(tmp_path):
    root = repository(tmp_path)

    def child_factory(spec):
        if spec.task_id == "implement-base":
            return FakeModelClient(
                [
                    ModelAction.tool(
                        "write_file",
                        {
                            "path": "shared.py",
                            "content": "first\n",
                            "expected_revision": "absent",
                        },
                    ),
                    ModelAction.final("created shared.py"),
                ]
            )
        return FakeModelClient(
            [
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "shared.py",
                        "old_text": "first\n",
                        "new_text": "second\n",
                        "expected_revision": content_revision(b"first\n"),
                    },
                ),
                ModelAction.final("updated shared.py"),
            ]
        )

    parent = build_parent(root, child_factory)
    result = parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-base",
                kind="implement",
                prompt="create shared.py",
                allowed_write_paths=("shared.py",),
            ),
            SubtaskSpec(
                task_id="implement-followup",
                kind="implement",
                prompt="update shared.py",
                depends_on=("implement-base",),
                allowed_write_paths=("shared.py",),
            ),
        )
    )

    assert [item["status"] for item in result["tasks"]] == [
        "completed",
        "completed",
    ]
    followup = parent.dependencies.subagents._worktrees[
        ("manual", "implement-followup")
    ].path
    assert Path(followup, "shared.py").read_text() == "second\n"

    applied = parent.dependencies.subagents.integration.apply(("implement-followup",))

    assert applied["task_ids"] == ["implement-base", "implement-followup"]
    assert (root / "shared.py").read_text() == "second\n"


def test_failed_integrated_verification_does_not_modify_parent(tmp_path):
    root = repository(tmp_path)

    class SelectiveSandbox(PassingSandbox):
        def run(self, *_args, **_kwargs):
            if self.root.name == "integration":
                return SandboxResult(returncode=1, stderr="verification failed\n")
            return SandboxResult(returncode=0, stdout="1 passed\n")

    parent = build_parent(
        root,
        lambda _spec: FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": "feature.py",
                        "content": "feature\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("implemented"),
            ]
        ),
        sandbox_factory=lambda child_root: SelectiveSandbox(child_root),
    )
    parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-feature",
                kind="implement",
                prompt="implement feature",
                allowed_write_paths=("feature.py",),
            ),
        )
    )

    with pytest.raises(RuntimeError, match="verification failed"):
        parent.dependencies.subagents.integration.apply(("implement-feature",))

    assert not (root / "feature.py").exists()


def test_dirty_parent_integration_error_names_the_integration_boundary(tmp_path):
    root = repository(tmp_path)
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": "feature.py",
                        "content": "feature\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("implemented"),
            ]
        ),
    )
    parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-dirty-parent",
                kind="implement",
                prompt="create feature.py",
                allowed_write_paths=("feature.py",),
            ),
        )
    )
    (root / "README.md").write_text("user changed parent\n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="parent workspace changed after implementation delegation",
    ):
        parent.dependencies.subagents.integration.apply(("implement-dirty-parent",))

    assert not (root / "feature.py").exists()


def test_parent_agent_delegates_and_applies_when_pico_state_is_untracked(tmp_path):
    root = repository(tmp_path, ignore_pico=False)
    task = {
        "task_id": "implement-parent-flow",
        "kind": "implement",
        "prompt": "create feature.py",
        "depends_on": [],
        "allowed_write_paths": ["feature.py"],
        "max_tool_executions": 4,
    }
    child_clients = []

    def child_factory(_spec):
        client = FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": "feature.py",
                        "content": "value = 1\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("implemented feature.py"),
            ]
        )
        child_clients.append(client)
        return client

    parent = build_parent(
        root,
        child_factory,
        parent_outputs=[
            ModelAction.tool(
                "delegate_tasks", {"tasks": [task]}, call_id="delegate-impl"
            ),
            ModelAction.final("premature success"),
            ModelAction.tool(
                "apply_task_patches",
                {"task_ids": ["implement-parent-flow"]},
                call_id="apply-impl",
            ),
            ModelAction.final("implemented and verified"),
        ],
    )

    answer = parent.ask("Implement feature.py using a child")

    assert answer == "implemented and verified"
    assert (root / "feature.py").read_text() == "value = 1\n"
    assert parent.run.evidence.changed_paths == ["feature.py"]
    assert parent.run.evidence.verifications[-1]["status"] == "passed"
    assert "delegate_tasks" not in child_clients[0].action_tool_surfaces[0]
    blocked = [
        entry
        for entry in parent.dependencies.run_store.read_events(parent.run.task_state.run_id)
        if entry.kind == "completion_blocked"
        and entry.payload["status"] == "subtasks_incomplete"
    ]
    assert len(blocked) == 1


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_failed_clean_check_does_not_leave_phantom_subtasks(tmp_path, dirty_kind):
    root = repository(tmp_path)
    if dirty_kind == "tracked":
        (root / "README.md").write_text("dirty\n", encoding="utf-8")
    else:
        (root / "user-note.txt").write_text("dirty\n", encoding="utf-8")
    task = {
        "task_id": "implement-dirty",
        "kind": "implement",
        "prompt": "create feature.py",
        "depends_on": [],
        "allowed_write_paths": ["feature.py"],
        "max_tool_executions": 4,
    }
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient([ModelAction.final("unused")]),
        parent_outputs=[
            ModelAction.tool(
                "delegate_tasks", {"tasks": [task]}, call_id="delegate-dirty"
            ),
            ModelAction.final("delegation was rejected"),
        ],
        verification_command="",
    )

    answer = parent.ask("Delegate despite a dirty workspace")

    assert answer == "delegation was rejected"
    assert parent.dependencies.subagents.completion_issue() == ""
    run_id = parent.run.task_state.run_id
    assert not (parent.dependencies.run_store.run_dir(run_id) / "subtasks.json").exists()
    blocked = [
        entry
        for entry in parent.dependencies.run_store.read_events(run_id)
        if entry.kind == "completion_blocked"
    ]
    assert blocked == []


def test_tampered_child_patch_is_rejected_before_integration(tmp_path):
    root = repository(tmp_path)
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": "safe.py",
                        "content": "safe\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("implemented"),
            ]
        ),
    )
    parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-tamper",
                kind="implement",
                prompt="create safe.py",
                allowed_write_paths=("safe.py",),
            ),
        )
    )
    record = parent.dependencies.subagents._records("manual")["implement-tamper"]
    Path(record.patch_path).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="patch digest is invalid"):
        parent.dependencies.subagents.integration.apply(("implement-tamper",))

    assert not (root / "safe.py").exists()


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("PICO_RUN_DOCKER_TESTS") != "1",
    reason="set PICO_RUN_DOCKER_TESTS=1",
)
def test_real_docker_verifies_child_and_integrated_worktrees(tmp_path):
    root = repository(tmp_path)
    (root / "verify.py").write_text(
        "from pathlib import Path\n"
        "assert Path('feature.py').read_text() == 'ok\\n'\n",
        encoding="utf-8",
    )
    git(root, "add", "verify.py")
    git(root, "commit", "-m", "add verifier")
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {
                        "path": "feature.py",
                        "content": "ok\n",
                        "expected_revision": "absent",
                    },
                ),
                ModelAction.final("implemented"),
            ]
        ),
        sandbox_factory=lambda child_root: DockerSandbox(child_root),
        verification_command="python verify.py",
    )
    receipt = parent.dependencies.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-docker",
                kind="implement",
                prompt="create feature.py",
                allowed_write_paths=("feature.py",),
            ),
        )
    )["tasks"][0]

    assert receipt["status"] == "completed"
    parent.dependencies.subagents.integration.apply(("implement-docker",))
    assert (root / "feature.py").read_text() == "ok\n"
