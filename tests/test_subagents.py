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
            "max_steps": 4,
        },
        {
            "task_id": "explore-cache",
            "kind": "explore",
            "prompt": "inspect cache",
            "depends_on": [],
            "allowed_write_paths": [],
            "max_steps": 4,
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
    receipts = payload["tasks"]
    assert {item["status"] for item in receipts} == {"completed"}
    assert len({item["child_session_id"] for item in receipts}) == 2
    assert len({item["child_run_ids"][0] for item in receipts}) == 2
    assert all("delegate_tasks" not in client.action_tool_surfaces[0] for client in clients)
    assert "delegate_tasks" in parent.model_client.action_tool_surfaces[0]


def test_delegate_tool_schema_is_strict_at_nested_task_level():
    schema = function_schema(DelegateTasksArgs)
    task_schema = schema["$defs"]["SubtaskSpec"]

    assert set(schema["required"]) == set(schema["properties"])
    assert set(task_schema["required"]) == set(task_schema["properties"])
    assert task_schema["additionalProperties"] is False


def test_legacy_subtask_state_is_rejected_without_migration(tmp_path):
    root = repository(tmp_path)
    parent = build_parent(
        root,
        lambda _spec: FakeModelClient([ModelAction.final("unused")]),
    )
    state_path = parent.services.run_store.run_dir("manual") / "subtasks.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "pico-subtasks-v1",
                "parent_run_id": "manual",
                "tasks": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported subtask state schema"):
        parent.services.subagents.delegate(
            (
                SubtaskSpec(
                    task_id="explore-new",
                    kind="explore",
                    prompt="inspect",
                ),
            )
        )


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
    result = parent.services.subagents.delegate(
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
        parent.services.subagents.delegate(
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
        parent.services.subagents.delegate(
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
        parent.services.subagents.delegate(
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


def test_implementation_worktrees_are_isolated_then_verified_and_applied(tmp_path):
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
                ModelAction.final(f"implemented {target}"),
            ]
        )

    parent = build_parent(root, child_factory)
    result = parent.services.subagents.delegate(
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
    assert not (root / "auth.py").exists()
    assert not (root / "cache.py").exists()
    worktrees = [
        parent.services.subagents._worktrees[("manual", task_id)].path
        for task_id in ("implement-auth", "implement-cache")
    ]
    assert worktrees[0] != worktrees[1]
    assert (worktrees[0] / "auth.py").exists()
    assert not (worktrees[0] / "cache.py").exists()
    assert (worktrees[1] / "cache.py").exists()
    assert not (worktrees[1] / "auth.py").exists()

    applied = parent.services.subagents.integration.apply(
        ("implement-auth", "implement-cache")
    )

    assert applied["status"] == "applied"
    assert applied["verification"]["exit_code"] == 0
    assert applied["verification"]["status"] == "passed"
    assert applied["verification"]["workspace_fingerprint"]
    assert applied["verification"]["verifier"] == "pytest"
    assert (root / "auth.py").read_text() == "implement-auth\n"
    assert (root / "cache.py").read_text() == "implement-cache\n"


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
    result = parent.services.subagents.delegate(
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

    assert receipt["status"] == "failed"
    assert not Path(receipt["patch_path"] or root / "missing").exists()
    record = parent.services.subagents._records("manual")["implement-safe"]
    run_store = RunStore(
        parent.services.run_store.run_dir("manual")
        / "subagents"
        / record.spec.task_id
        / "runs"
    )
    entries = run_store.read_entries(record.child_run_ids[0])
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
    receipt = parent.services.subagents.delegate(
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


def test_continue_task_reuses_child_session_and_prior_summary(tmp_path):
    root = repository(tmp_path)
    clients = []

    def child_factory(_spec):
        answer = "first result" if not clients else "continued result"
        client = FakeModelClient([ModelAction.final(answer)])
        clients.append(client)
        return client

    parent = build_parent(root, child_factory)
    first = parent.services.subagents.delegate(
        (
            SubtaskSpec(
                task_id="explore-session",
                kind="explore",
                prompt="first question",
            ),
        )
    )["tasks"][0]
    continued = parent.services.subagents.continue_task(
        "explore-session", "follow-up question"
    )

    assert continued["child_session_id"] == first["child_session_id"]
    assert len(continued["child_run_ids"]) == 2
    assert continued["continuation_count"] == 1
    assert "result: first result" in clients[1].prompts[0]


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

    parent.services.subagents.delegate((spec,))
    second = parent.services.subagents.delegate((spec,))

    assert calls == 1
    assert second["tasks"][0]["reused"] is True
    record = parent.services.subagents._records("manual")["explore-reuse"]
    assert record.status == "finished"
    assert "result" not in record.model_dump()
    assert "worktree" not in record.model_dump()


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
                    "patch_file",
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
    result = parent.services.subagents.delegate(
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
    followup = parent.services.subagents._worktrees[
        ("manual", "implement-followup")
    ].path
    assert Path(followup, "shared.py").read_text() == "second\n"

    applied = parent.services.subagents.integration.apply(("implement-followup",))

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
    parent.services.subagents.delegate(
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
        parent.services.subagents.integration.apply(("implement-feature",))

    assert not (root / "feature.py").exists()


def test_parent_agent_delegates_and_applies_when_pico_state_is_untracked(tmp_path):
    root = repository(tmp_path, ignore_pico=False)
    task = {
        "task_id": "implement-parent-flow",
        "kind": "implement",
        "prompt": "create feature.py",
        "depends_on": [],
        "allowed_write_paths": ["feature.py"],
        "max_steps": 4,
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
        for entry in parent.services.run_store.read_entries(parent.run.task_state.run_id)
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
        "max_steps": 4,
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
    assert parent.services.subagents.completion_issue() == ""
    run_id = parent.run.task_state.run_id
    assert not (parent.services.run_store.run_dir(run_id) / "subtasks.json").exists()
    blocked = [
        entry
        for entry in parent.services.run_store.read_entries(run_id)
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
    receipt = parent.services.subagents.delegate(
        (
            SubtaskSpec(
                task_id="implement-tamper",
                kind="implement",
                prompt="create safe.py",
                allowed_write_paths=("safe.py",),
            ),
        )
    )["tasks"][0]
    Path(receipt["patch_path"]).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="patch digest is invalid"):
        parent.services.subagents.integration.apply(("implement-tamper",))

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
    receipt = parent.services.subagents.delegate(
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
    parent.services.subagents.integration.apply(("implement-docker",))
    assert (root / "feature.py").read_text() == "ok\n"
