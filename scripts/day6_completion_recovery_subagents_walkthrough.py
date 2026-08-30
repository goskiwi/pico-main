"""Day 6: exercise completion policy, recovery, reset, and subagent scope."""

import json
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
    WorkspaceContext,
)
from pico.completion_controller import CompletionController
from pico.execution import ExecutionContext
from pico.mutations import file_revision
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunLog
from pico.run_projection import RunProjection
from pico.sandbox import SandboxResult
from pico.subagents.contracts import SubtaskRecord, SubtaskSpec
from pico.subagents.dag import (
    implementation_order,
    ready_task_ids,
    validate_graph,
)
from pico.task_state import TaskContract
from pico.verification import capture_changed_path_states


class SequenceSandbox:
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
    requires_workspace_change=False,
    requires_verification=False,
    allowed_write_paths=None,
):
    contract = TaskContract(
        goal=goal,
        task_kind="modify",
        requires_workspace_change=requires_workspace_change,
        requires_verification=requires_verification,
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
    return agent.tools.execute(call)


def completion_experiment(root):
    target = root / "subject.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    sandbox = SequenceSandbox(
        [
            SandboxResult(returncode=0, stdout="1 passed\n"),
            SandboxResult(returncode=1, stdout="1 failed\n"),
            SandboxResult(returncode=0, stdout="1 passed\n"),
        ]
    )
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="verify",
        ),
        sandbox=sandbox,
    )
    activate(
        agent,
        "task_day6_completion",
        "run_day6_completion",
        "Change value and verify it",
        requires_workspace_change=True,
        requires_verification=True,
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
    assert revert_assessment.status == "workspace_change_required"
    assert final_edit.status == "success"
    assert final_cursor > revert_cursor
    assert old_verification_for_final_state is None
    assert failed_assessment.allowed is False
    assert failed_assessment.status == "verification_failed"
    assert passed_assessment.allowed is True
    assert len(sandbox.calls) == 3

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
        "verification_runs": len(sandbox.calls),
        "first_change": {
            "mutation_sequence": first_cursor,
        },
        "verifications": agent.run.evidence.verifications,
    }


def recovery_experiment(root):
    store = SessionStore(root / ".pico" / "sessions")
    original = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=store,
        config=PicoConfig(approval_policy="auto", verification_command="verify"),
    )
    RunLifecycle(original).initialize(
        "Create interrupted.txt",
        task_kind="modify",
        requires_workspace_change=True,
        requires_verification=False,
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
            risky=True,
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
    sandbox = SequenceSandbox(
        [SandboxResult(returncode=0, stdout="1 passed\n")]
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
        workspace=WorkspaceContext.build(root),
        session_store=store,
        session=loaded_session,
        config=PicoConfig(
            approval_policy="auto",
            verification_command="verify",
        ),
        sandbox=sandbox,
    )
    assert resumed.run.resumable is True
    startup_state = {
        "resumable": resumed.run.resumable,
        "reload_required": resumed.run.reload_required,
        "pending_call_id": resumed.run.projection.pending_call_id,
    }

    run_outcome = resumed.ask(
        "Continue after the crash",
        task_kind="modify",
        requires_workspace_change=True,
        requires_verification=False,
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
    assert len(sandbox.calls) == 1
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
            "verification_count": len(sandbox.calls),
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
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(approval_policy="auto", verification_command=""),
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
                task_kind="modify",
                requires_workspace_change=True,
                requires_verification=False,
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


def subagent_dag_experiment(root):
    specs = (
        SubtaskSpec(
            task_id="explore_api",
            kind="explore",
            prompt="Inspect the public API",
        ),
        SubtaskSpec(
            task_id="explore_tests",
            kind="explore",
            prompt="Inspect the relevant tests",
        ),
        SubtaskSpec(
            task_id="implement_fix",
            kind="implement",
            prompt="Implement the synthesized fix",
            depends_on=("explore_api", "explore_tests"),
            allowed_write_paths=("subject.py",),
        ),
    )
    validate_graph({}, specs)
    records = {spec.task_id: SubtaskRecord(spec) for spec in specs}
    targets = set(records)
    first_ready = ready_task_ids(
        records,
        targets,
        completed_ids=set(),
        failed_ids=set(),
    )
    for task_id in first_ready:
        records[task_id].status = "completed"
    second_ready = ready_task_ids(
        records,
        targets,
        completed_ids=set(first_ready),
        failed_ids=set(),
    )
    order = implementation_order(records, ("implement_fix",))

    conflict = ""
    try:
        validate_graph(
            {},
            (
                SubtaskSpec(
                    task_id="implement_a",
                    kind="implement",
                    prompt="first writer",
                    allowed_write_paths=("shared.py",),
                ),
                SubtaskSpec(
                    task_id="implement_b",
                    kind="implement",
                    prompt="second writer",
                    allowed_write_paths=("shared.py",),
                ),
            ),
        )
    except ValueError as exc:
        conflict = str(exc)

    assert first_ready == ["explore_api", "explore_tests"]
    assert second_ready == ["implement_fix"]
    assert order == ("implement_fix",)
    assert "overlapping write paths" in conflict

    parent = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        config=PicoConfig(
            approval_policy="auto",
            verification_command="verify",
            allowed_write_paths=("subject.py", "other.py"),
        ),
        subagent_model_client_factory=lambda _spec: FakeModelClient([]),
    )
    activate(
        parent,
        "task_day6_subagent_scope",
        "run_day6_subagent_scope",
        "Delegate only the permitted file",
        allowed_write_paths=("subject.py",),
    )
    inside_spec = {
        "task_id": "implement_inside",
        "kind": "implement",
        "prompt": "Edit the permitted file",
        "allowed_write_paths": ["subject.py"],
    }
    outside_spec = {
        "task_id": "implement_outside",
        "kind": "implement",
        "prompt": "Edit a file outside the parent contract",
        "allowed_write_paths": ["other.py"],
    }
    validated_inside = parent.tools.validate(
        "delegate_tasks",
        {"tasks": [inside_spec]},
    )
    delegation_rejection = ""
    try:
        parent.tools.validate("delegate_tasks", {"tasks": [outside_spec]})
    except ValueError as exc:
        delegation_rejection = str(exc)

    manager = parent.dependencies.subagents
    assert manager is not None
    inside_record = SubtaskRecord(
        SubtaskSpec.model_validate(inside_spec),
        status="completed",
        changed_paths=("subject.py",),
    )
    manager._records_by_run[parent.run.projection.run_id] = {
        inside_record.spec.task_id: inside_record
    }
    validated_apply = parent.tools.validate(
        "apply_task_patches",
        {"task_ids": [inside_record.spec.task_id]},
    )

    synthetic_outside_receipt = SubtaskRecord(
        SubtaskSpec.model_validate(outside_spec),
        status="completed",
        changed_paths=("other.py",),
    )
    manager._records_by_run[parent.run.projection.run_id] = {
        synthetic_outside_receipt.spec.task_id: synthetic_outside_receipt
    }
    apply_rejection = ""
    try:
        parent.tools.validate(
            "apply_task_patches",
            {"task_ids": [synthetic_outside_receipt.spec.task_id]},
        )
    except ValueError as exc:
        apply_rejection = str(exc)

    assert validated_inside["tasks"][0]["allowed_write_paths"] == (
        "subject.py",
    )
    assert validated_apply["task_ids"] == ("implement_inside",)
    assert "other.py" in delegation_rejection
    assert "other.py" in apply_rejection
    contract_scope = parent.run.task.contract.allowed_write_paths
    config_scope = parent.config.allowed_write_paths
    effective_scope = tuple(
        path
        for path in contract_scope
        if config_scope is None or path in set(config_scope)
    )
    assert effective_scope == ("subject.py",)

    return {
        "parallel_first_batch": first_ready,
        "ready_after_dependencies": second_ready,
        "implementation_order": list(order),
        "rejected_parallel_write_conflict": conflict,
        "parent_scope": {
            "config_allows": ["subject.py", "other.py"],
            "task_contract_allows": ["subject.py"],
            "effective_scope": list(effective_scope),
            "delegate_inside_scope": "accepted",
            "delegate_outside_scope": delegation_rejection,
            "apply_inside_scope": "accepted",
            "apply_defensive_recheck": {
                "fixture": (
                    "synthetic corrupted-or-reloaded completed receipt; this "
                    "outside path cannot pass normal delegate admission"
                ),
                "outside_scope_rejection": apply_rejection,
            },
        },
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
            "Subagent DAG 与父级写范围（附录）",
            subagent_dag_experiment(subagent_root),
        )


if __name__ == "__main__":
    main()
