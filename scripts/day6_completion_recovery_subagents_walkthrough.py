"""Day 6: exercise completion checks, crash recovery, and subtask DAGs."""

import json
import tempfile
from pathlib import Path

from pico import (
    FakeModelClient,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    WorkspaceContext,
)
from pico.completion_controller import CompletionController
from pico.evidence import verification_is_current
from pico.execution import ExecutionContext
from pico.mutations import file_revision
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
    requires_verification=False,
):
    contract = TaskContract(
        goal=goal,
        task_kind="modify",
        requires_workspace_change=False,
        requires_verification=requires_verification,
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
        requires_verification=True,
    )

    first_edit = apply_edit(agent, "call_edit_first", "return 1", "return 2")
    first_assessment = CompletionController(agent).assess("first final")
    first_cursor = agent.run.evidence.last_workspace_mutation_sequence

    second_edit = apply_edit(agent, "call_edit_second", "return 2", "return 3")
    prior_verification = agent.run.evidence.verifications[0]
    second_cursor = agent.run.evidence.last_workspace_mutation_sequence
    current_states = capture_changed_path_states(
        agent.workspace.root,
        agent.run.evidence.changed_paths,
    )
    stale_after_second_edit = not verification_is_current(
        prior_verification,
        second_cursor,
        current_states,
    )
    second_assessment = CompletionController(agent).assess("second final")

    assert first_edit.status == "success"
    assert first_assessment.allowed is True
    assert second_edit.status == "success"
    assert second_cursor > first_cursor
    assert stale_after_second_edit is True
    assert second_assessment.allowed is False
    assert second_assessment.status == "verification_failed"
    assert len(sandbox.calls) == 2

    return {
        "first_completion": {
            "allowed": first_assessment.allowed,
            "mutation_sequence": first_cursor,
        },
        "after_second_mutation": {
            "old_verification_is_stale": stale_after_second_edit,
            "new_mutation_sequence": second_cursor,
            "completion_allowed": second_assessment.allowed,
            "completion_status": second_assessment.status,
        },
        "verifications": agent.run.evidence.verifications,
    }


def recovery_experiment(root):
    store = SessionStore(root / ".pico" / "sessions")
    original = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=store,
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    _state, run_log = activate(
        original,
        "task_day6_recovery",
        "run_day6_recovery",
        "Create interrupted.txt",
    )
    original.session.set_active_run(original.run.projection.run_id)
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
    resumed = Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=store,
        session=loaded_session,
        config=PicoConfig(approval_policy="auto", verification_command=""),
    )
    assert resumed.run.resumable is True
    restored = resumed.run.run_log
    reconciled = restored.reconcile_interrupted(resumed)
    for _outcome, event in reconciled:
        resumed.apply_run_event(event)
    outcome, event = reconciled[-1]

    assert outcome.status == "partial_success"
    assert outcome.side_effect_state == "partial"
    assert outcome.affected_paths == ("interrupted.txt",)
    assert event.payload["recovered_from_interruption"] is True
    assert (root / "interrupted.txt").read_text(encoding="utf-8") == (
        "side effect happened\n"
    )

    return {
        "recovery_status": "resumable" if resumed.run.resumable else "active",
        "outcome": outcome.to_dict(),
        "recovered_from_interruption": event.payload[
            "recovered_from_interruption"
        ],
        "file_content_after_recovery": (
            root / "interrupted.txt"
        ).read_text(encoding="utf-8"),
    }


def subagent_dag_experiment():
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

    return {
        "parallel_first_batch": first_ready,
        "ready_after_dependencies": second_ready,
        "implementation_order": list(order),
        "rejected_parallel_write_conflict": conflict,
    }


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day6-") as directory:
        root = Path(directory)
        completion_root = root / "completion"
        recovery_root = root / "recovery"
        completion_root.mkdir()
        recovery_root.mkdir()

        print_section(
            "Completion Gate 与 verification freshness",
            completion_experiment(completion_root),
        )
        print_section(
            "崩溃恢复不重放副作用",
            recovery_experiment(recovery_root),
        )
        print_section("Subagent DAG", subagent_dag_experiment())


if __name__ == "__main__":
    main()
