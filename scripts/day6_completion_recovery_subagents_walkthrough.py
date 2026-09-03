"""Day 6: exercise completion, recovery, reset, and explicit Child integration."""

import json
import subprocess
import tempfile
import threading
from pathlib import Path

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    Workspace,
)
from pico.command_runner import CommandResult
from pico.completion_controller import CompletionController
from pico.execution import ExecutionContext
from pico.mutations import file_revision
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.task_state import TaskContract
from pico.verification import capture_changed_path_states


class SequenceCommandRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": sorted(kwargs)})
        return self.results.pop(0)


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def activate(
    agent,
    task_id,
    run_id,
    goal,
    *,
    verify_changes=False,
    allowed_write_paths=None,
):
    contract = TaskContract(
        goal=goal,
        allows_workspace_mutation=True,
        verify_changes=verify_changes,
        allowed_write_paths=allowed_write_paths,
    )
    run_log = RunLog(
        run_id,
        task_id,
        agent.session.data["id"],
        agent.dependencies.run_store,
    )
    agent.run.run_log = run_log
    first = run_log.append_user(contract)
    agent.run.projection = RunProjection().apply_event(first)
    agent.run.execution_context = ExecutionContext.root(max_seconds=30)
    return agent.run.task, run_log


def apply_edit(agent, call_id, old_text, new_text):
    target = agent.workspace.root / "subject.py"
    call = ToolCall(
        "edit_file",
        {
            "path": "subject.py",
            "old_text": old_text,
            "new_text": new_text,
            "expected_revision": file_revision(target),
        },
        call_id,
    )
    agent.apply_run_event(agent.run.run_log.append_tool_call(call))
    return agent.tools.execute_pending(call.call_id)


def completion_experiment(root):
    target = root / "subject.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    command_runner = SequenceCommandRunner(
        [
            CommandResult(returncode=0, stdout="1 passed\n"),
            CommandResult(returncode=1, stdout="1 failed\n"),
            CommandResult(returncode=0, stdout="1 passed\n"),
        ]
    )
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=Workspace.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(
            mode="auto",
            verification_command="verify",
        ),
        command_runner=command_runner,
    )
    activate(
        agent,
        "task_day6_completion",
        "run_day6_completion",
        "Change value and verify it",
        verify_changes=True,
    )

    # Evidence first records facts for A -> B and a passing verification.
    first_edit = apply_edit(agent, "call_edit_first", "return 1", "return 2")
    first_cursor = agent.run.evidence.last_workspace_mutation_sequence
    first_verification = agent.run_verification(first_cursor)
    assert first_verification is not None
    agent.emit_event("verification_result", first_verification)
    first_states = capture_changed_path_states(
        agent.workspace.root,
        agent.run.evidence.changed_paths,
    )
    first_current = agent.run.evidence.latest_verification_for_state(
        first_cursor,
        first_states,
        agent.config.verification_command,
    )
    net_change_after_first_edit = agent.run.evidence.has_net_workspace_change

    # B -> A removes the net change. The old verification is now a stale fact,
    # and CompletionController (not Evidence) decides that the contract blocks.
    revert_edit = apply_edit(agent, "call_edit_revert", "return 2", "return 1")
    revert_cursor = agent.run.evidence.last_workspace_mutation_sequence
    revert_states = capture_changed_path_states(
        agent.workspace.root,
        agent.run.evidence.changed_paths,
    )
    current_after_revert = agent.run.evidence.latest_verification_for_state(
        revert_cursor,
        revert_states,
        agent.config.verification_command,
    )
    net_change_after_revert = agent.run.evidence.has_net_workspace_change
    revert_assessment = CompletionController(agent).assess("final after revert")

    # A -> C restores a net change. The first completion attempt runs a failing
    # verifier; a second attempt on the same state retries it and passes.
    final_edit = apply_edit(agent, "call_edit_final", "return 1", "return 3")
    final_cursor = agent.run.evidence.last_workspace_mutation_sequence
    final_states = capture_changed_path_states(
        agent.workspace.root,
        agent.run.evidence.changed_paths,
    )
    old_verification_for_final_state = (
        agent.run.evidence.latest_verification_for_state(
            final_cursor,
            final_states,
            agent.config.verification_command,
        )
    )
    failed_assessment = CompletionController(agent).assess("final after C")
    passed_assessment = CompletionController(agent).assess("final after C")

    assert first_edit.status == "success"
    assert first_current is not None
    assert net_change_after_first_edit is True
    assert revert_edit.status == "success"
    assert revert_cursor > first_cursor
    assert net_change_after_revert is False
    assert current_after_revert is None
    assert revert_assessment.allowed is False
    assert revert_assessment.status == "observation_required"
    assert final_edit.status == "success"
    assert final_cursor > revert_cursor
    assert old_verification_for_final_state is None
    assert failed_assessment.allowed is False
    assert failed_assessment.status == "verification_failed"
    assert passed_assessment.allowed is True
    assert len(command_runner.calls) == 3

    return {
        "evidence_facts": {
            "after_A_to_B": {
                "has_net_workspace_change": net_change_after_first_edit,
                "verification_is_current": first_current is not None,
            },
            "after_B_to_A": {
                "has_net_workspace_change": net_change_after_revert,
                "verification_is_current": current_after_revert is not None,
                "mutation_sequence": revert_cursor,
            },
            "after_A_to_C": {
                "has_net_workspace_change": (
                    agent.run.evidence.has_net_workspace_change
                ),
                "old_verification_is_current": (
                    old_verification_for_final_state is not None
                ),
                "mutation_sequence": final_cursor,
            },
        },
        "completion_decisions": {
            "after_net_revert": {
                "allowed": revert_assessment.allowed,
                "status": revert_assessment.status,
            },
            "first_attempt_on_C": {
                "allowed": failed_assessment.allowed,
                "status": failed_assessment.status,
            },
            "retry_on_same_C": {
                "allowed": passed_assessment.allowed,
                "status": passed_assessment.status or "allowed",
            },
        },
        "verification_runs": len(command_runner.calls),
        "first_change": {
            "mutation_sequence": first_cursor,
        },
        "verifications": agent.run.evidence.verifications,
    }


def recovery_experiment(root):
    store = SessionStore(root / ".pico" / "sessions")
    original = Pico(
        model_client=FakeModelClient([]),
        workspace=Workspace.build(root),
        session_store=store,
        config=PicoConfig(mode="auto", verification_command="verify"),
    )
    RunLifecycle(original).initialize(
        "Create interrupted.txt",
    )
    run_log = original.run.run_log
    assert run_log is not None
    call = ToolCall(
        "write_file",
        {
            "path": "interrupted.txt",
            "content": "side effect happened\n",
        },
        "call_interrupted_write",
    )
    original.apply_run_event(run_log.append_tool_call(call))
    original.apply_run_event(
        run_log.append_tool_started(
            call,
            effect_scope="workspace",
            potential_effects=[
                {
                    "path": "interrupted.txt",
                    "before_state": "absent",
                    "before_artifact_id": "",
                }
            ],
        )
    )
    (root / "interrupted.txt").write_text(
        "side effect happened\n",
        encoding="utf-8",
    )

    loaded_session = store.load(original.session.data["id"])
    target = root / "interrupted.txt"
    repair_revision = file_revision(target)
    command_runner = SequenceCommandRunner(
        [CommandResult(returncode=0, stdout="1 passed\n")]
    )
    resumed = Pico(
        model_client=FakeModelClient(
            [
                ModelAction.tool(
                    "read_file",
                    {
                        "path": "interrupted.txt",
                        "start_line": 1,
                        "end_line": 20,
                    },
                    call_id="call_recovery_read",
                ),
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "interrupted.txt",
                        "old_text": "side effect happened",
                        "new_text": "side effect confirmed",
                        "expected_revision": repair_revision,
                    },
                    call_id="call_recovery_repair",
                ),
                ModelAction.final("Recovered, inspected, repaired, and verified."),
            ]
        ),
        workspace=Workspace.build(root),
        session_store=store,
        session=loaded_session,
        config=PicoConfig(
            mode="auto",
            verification_command="verify",
        ),
        command_runner=command_runner,
    )
    assert resumed.run.resumable is True
    startup_state = {
        "resumable": resumed.run.resumable,
        "pending_call_ids": list(resumed.run.projection.pending_call_ids),
    }

    run_outcome = resumed.ask(
        "Continue after the crash",
    )
    events = resumed.dependencies.run_store.read_events(run_outcome.run_id)
    recovered_results = [
        event
        for event in events
        if event.kind == "tool_result"
        and event.call_id == "call_interrupted_write"
        and event.payload.get("recovered_from_interruption") is True
    ]
    original_calls = [
        event
        for event in events
        if event.kind == "assistant_tool_call"
        and event.call_id == "call_interrupted_write"
    ]
    original_starts = [
        event
        for event in events
        if event.kind == "tool_started"
        and event.call_id == "call_interrupted_write"
    ]
    resumed_events = [event for event in events if event.kind == "run_resumed"]
    started_run_events = [event for event in events if event.kind == "run_started"]
    recovered_outcome = recovered_results[0].payload["outcome"]

    assert recovered_outcome["status"] == "partial_success"
    assert recovered_outcome["side_effect_state"] == "partial"
    assert recovered_outcome["affected_paths"] == ["interrupted.txt"]
    assert len(original_calls) == len(original_starts) == 1
    assert len(recovered_results) == len(resumed_events) == 1
    assert len(started_run_events) == 1
    assert run_outcome.status == "completed"
    assert len(command_runner.calls) == 1
    assert target.read_text(encoding="utf-8") == "side effect confirmed\n"
    assert resumed.run.evidence.unresolved_effects(
        resumed.run.evidence.verifications[-1]
    ) == []

    return {
        "startup_active_run_state": startup_state,
        "automatic_reconciliation": {
            "run_started_count": len(started_run_events),
            "run_resumed_count": len(resumed_events),
            "synthetic_crash_point": (
                "only the missing-result call/tool_started prefix and its observed "
                "filesystem side effect are injected; Run creation and Resume use "
                "production RunLifecycle"
            ),
            "recovered_tool_outcome": recovered_outcome,
            "original_call_count": len(original_calls),
            "original_tool_started_count": len(original_starts),
            "blind_replay_occurred": len(original_starts) != 1,
        },
        "repair_and_verification": {
            "file_content": target.read_text(encoding="utf-8"),
            "verification_count": len(command_runner.calls),
            "unresolved_effects": resumed.run.evidence.unresolved_effects(
                resumed.run.evidence.verifications[-1]
            ),
        },
        "run_outcome": run_outcome.to_dict(),
    }


def active_reset_experiment(root):
    started = threading.Event()
    release = threading.Event()
    agent = Pico(
        model_client=FakeModelClient(
            [
                ModelAction.tool(
                    "write_file",
                    {"path": "late.txt", "content": "tracked side effect\n"},
                    call_id="call_late_write",
                ),
                ModelAction.final("This final action must not run."),
            ]
        ),
        workspace=Workspace.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(mode="auto", verification_command=""),
    )
    original_runner = agent.tools.registry["write_file"]["run"]

    def blocked_runner(args):
        started.set()
        if not release.wait(timeout=3):
            raise TimeoutError("reset walkthrough runner was not released")
        return original_runner(args)

    agent.tools.registry["write_file"]["run"] = blocked_runner
    result = {}

    def ask_in_thread():
        try:
            result["outcome"] = agent.ask(
                "Create late.txt",
            )
        except BaseException as exc:  # noqa: BLE001 - thread handoff
            result["error"] = exc

    thread = threading.Thread(target=ask_in_thread)
    thread.start()
    assert started.wait(timeout=3)
    run_id = agent.run.projection.run_id

    agent.reset()
    state_preserved_until_runner_finishes = agent.run.task is not None
    reset_reason = agent.run.execution_context.token.reason
    release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert "error" not in result
    run_outcome = result["outcome"]
    events = agent.dependencies.run_store.read_events(run_id)
    assert reset_reason == "user_reset"
    assert state_preserved_until_runner_finishes is True
    assert [event.kind for event in events[-2:]] == [
        "tool_result",
        "run_stopped",
    ]
    assert run_outcome.status == "stopped"
    assert run_outcome.stop_reason == "user_reset"
    assert (root / "late.txt").read_text(encoding="utf-8") == (
        "tracked side effect\n"
    )
    assert agent.run.task is None
    assert agent.session.data["active_run_id"] == ""

    return {
        "reset_requested_while_runner_active": reset_reason,
        "active_state_preserved_until_tool_result": (
            state_preserved_until_runner_finishes
        ),
        "terminal_event_order": [event.kind for event in events[-2:]],
        "run_outcome": run_outcome.to_dict(),
        "active_run_state_cleared": agent.run.task is None,
    }


def _git(root, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def _passing_runner(_root):
    return SequenceCommandRunner(
        [CommandResult(returncode=0, stdout="verification passed\n")]
    )


def _execute_parent_tool(parent, name, args, call_id):
    call = ToolCall(name, args, call_id)
    parent.apply_run_event(parent.run.run_log.append_tool_call(call))
    return parent.tools.execute_pending(call.call_id)


def child_delegation_experiment(root):
    target = root / "subject.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".pico/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "pico@example.invalid")
    _git(root, "config", "user.name", "Pico")
    _git(root, "add", "subject.py", ".gitignore")
    _git(root, "commit", "-qm", "base")

    expected_revision = file_revision(target)
    child_clients = []

    def child_factory(spec):
        if spec.role == "explore":
            outputs = [
                ModelAction.tool(
                    "read_file",
                    {"path": "subject.py", "start_line": 1, "end_line": 20},
                    call_id="child_explore_read",
                ),
                ModelAction.final("subject.py contains value() returning 1."),
            ]
        else:
            outputs = [
                ModelAction.tool(
                    "read_file",
                    {"path": "subject.py", "start_line": 1, "end_line": 20},
                    call_id="child_implement_read",
                ),
                ModelAction.tool(
                    "edit_file",
                    {
                        "path": "subject.py",
                        "old_text": "return 1",
                        "new_text": "return 2",
                        "expected_revision": expected_revision,
                    },
                    call_id="child_implement_edit",
                ),
                ModelAction.final("Changed value() to return 2."),
            ]
        client = FakeModelClient(outputs)
        child_clients.append(client)
        return client

    parent = Pico(
        model_client=FakeModelClient([]),
        workspace=Workspace.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(
            mode="auto",
            verification_command="verify",
            allowed_write_paths=("subject.py",),
        ),
        command_runner_factory=_passing_runner,
        subagent_model_client_factory=child_factory,
    )
    activate(
        parent,
        "task_day6_children",
        "run_day6_children",
        "Inspect, implement, and explicitly integrate subject.py",
        verify_changes=True,
        allowed_write_paths=("subject.py",),
    )

    explore = _execute_parent_tool(
        parent,
        "delegate",
        {
            "role": "explore",
            "task": "Inspect subject.py and report the exact implementation fact.",
            "allowed_write_paths": [],
        },
        "call_delegate_explore",
    )
    implement = _execute_parent_tool(
        parent,
        "delegate",
        {
            "role": "implement",
            "task": "Change value() in subject.py to return 2.",
            "allowed_write_paths": ["subject.py"],
        },
        "call_delegate_implement",
    )
    child_id = implement.structured["child_id"]
    parent_before_integration = target.read_text(encoding="utf-8")
    blocked = CompletionController(parent).assess("done before integration")

    integrated = _execute_parent_tool(
        parent,
        "integrate_child",
        {"child_id": child_id},
        "call_integrate_child",
    )
    parent_after_integration = target.read_text(encoding="utf-8")
    completed = CompletionController(parent).assess("done after integration")

    assert explore.status == "success"
    assert explore.structured["role"] == "explore"
    assert explore.structured["changed_paths"] == []
    assert implement.status == "success"
    assert implement.structured["role"] == "implement"
    assert implement.structured["changed_paths"] == ["subject.py"]
    assert implement.structured["integrated"] is False
    assert parent_before_integration == "def value():\n    return 1\n"
    assert blocked.status == "subtasks_incomplete"
    assert integrated.status == "success"
    assert integrated.structured["status"] == "integrated"
    assert integrated.structured["changed_paths"] == ["subject.py"]
    assert integrated.structured["verification"]["status"] == "passed"
    assert parent_after_integration == "def value():\n    return 2\n"
    assert parent.run.evidence.changed_paths == ["subject.py"]
    assert completed.allowed
    assert all(
        "delegate" not in client.action_tool_surfaces[0]
        and "integrate_child" not in client.action_tool_surfaces[0]
        for client in child_clients
    )

    return {
        "explore": {
            "child_id": explore.structured["child_id"],
            "status": explore.structured["status"],
            "ask_mode": explore.structured["changed_paths"] == [],
        },
        "implement": {
            "child_id": child_id,
            "status": implement.structured["status"],
            "base_sha": implement.structured["base_sha"],
            "changed_paths": implement.structured["changed_paths"],
            "patch_sha256": implement.structured["patch_sha256"],
            "parent_unchanged_before_integration": (
                "return 1" in parent_before_integration
            ),
        },
        "completion_before_integration": blocked.status,
        "integration": {
            "status": integrated.structured["status"],
            "base_revalidated": integrated.structured["base_sha"]
            == implement.structured["base_sha"],
            "verification": integrated.structured["verification"]["status"],
            "parent_changed_after_explicit_action": (
                "return 2" in parent_after_integration
            ),
        },
        "child_tool_surface_excludes_nested_delegation": True,
        "completion_after_integration": "allowed",
    }


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day6-") as directory:
        root = Path(directory)
        completion_root = root / "completion"
        recovery_root = root / "recovery"
        reset_root = root / "active-reset"
        subagent_root = root / "subagent"
        completion_root.mkdir()
        recovery_root.mkdir()
        reset_root.mkdir()
        subagent_root.mkdir()

        print_section(
            "Completion Gate 与 verification freshness",
            completion_experiment(completion_root),
        )
        print_section(
            "崩溃恢复不重放副作用",
            recovery_experiment(recovery_root),
        )
        print_section(
            "Active reset 等待 Tool Result 后再停止",
            active_reset_experiment(reset_root),
        )
        print_section(
            "单 Child 委派与显式集成（附录）",
            child_delegation_experiment(subagent_root),
        )


if __name__ == "__main__":
    main()
