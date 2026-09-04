"""Day 2: inspect Run Log facts, Projection replay, and safe resume.

The three experiments keep separate questions separate:

1. What is durably stored, and what is derived?
2. How do one Pending Call and one Observation Batch change across legal transactions?
3. What happens when a process stops after ``tool_started`` but before a result?
"""

import json
import tempfile
from pathlib import Path

from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    RunOutcome,
    SessionStore,
    ToolCall,
    Workspace,
)
from pico.contracts import ToolOutcome
from pico.run_lifecycle import RunLifecycle
from pico.run_log import replay_events


def print_section(title, value):
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def build_agent(root, model, *, session=None):
    store = SessionStore(root / ".pico" / "sessions")
    runtime_workspace = Workspace.build(root)
    return Pico(
        model_client=model,
        workspace=runtime_workspace,
        config=PicoConfig(mode="ask", verification_command=""),
        session=session
        if session is not None
        else store.create(runtime_workspace.root),
    )


def fact_projection_experiment(root):
    """Create one Run, then rebuild every derived field from its facts."""

    (root / "README.md").write_text(
        "# State demo\n\nThe durable Run Log owns process facts.\n",
        encoding="utf-8",
    )
    model = FakeModelClient(
        [
            ModelAction.tool(
                "update_working_state",
                {
                    "add_constraints": ["Do not modify the workspace"],
                    "add_decisions": ["Inspect README through a file tool"],
                    "add_next_steps": ["Read README.md"],
                },
                call_id="call_working_state_add",
            ),
            ModelAction.tool(
                "read_file",
                {"path": "README.md", "start_line": 1, "end_line": 20},
                call_id="call_readme",
            ),
            ModelAction.tool(
                "update_working_state",
                {
                    "add_decisions": ["README.md was inspected"],
                    "remove_constraints": ["Do not modify the workspace"],
                    "remove_decisions": [
                        "Inspect README through a file tool"
                    ],
                    "remove_next_steps": ["Read README.md"],
                },
                call_id="call_working_state_remove",
            ),
            ModelAction.final("State walkthrough completed."),
        ]
    )
    agent = build_agent(root, model)
    outcome = agent.ask(
        "Inspect README without changing the workspace",
    )
    run_id = outcome.run_id
    store = agent.dependencies.run_store
    events, loaded = store.load_run(run_id)
    replayed = store.replay(run_id)

    live_summary = agent.run.projection.summary()
    loaded_summary = loaded.summary()
    replayed_summary = replayed.summary()
    assert live_summary == loaded_summary == replayed_summary
    assert [event.sequence for event in events] == list(
        range(1, len(events) + 1)
    )

    raw_lines = [
        json.loads(line)
        for line in store.events_path(run_id).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    raw_result = next(
        value
        for value in raw_lines
        if value["kind"] == "tool_result"
        and value["payload"]["outcome"]["tool_name"] == "read_file"
    )
    assert set(raw_result["payload"]) == {"outcome"}
    assert "tool_call_id" not in raw_result["payload"]
    assert "tool_name" not in raw_result["payload"]

    working_state_calls = [
        event
        for event in events
        if event.kind == "assistant_tool_call"
        and event.name == "update_working_state"
    ]
    expected_delta_fields = {
        "add_constraints",
        "remove_constraints",
        "add_decisions",
        "remove_decisions",
        "add_next_steps",
        "remove_next_steps",
    }
    observed_delta_fields = {
        field
        for event in working_state_calls
        for field in event.args
    }
    final_working_state = replayed.task.working.to_dict()
    assert len(working_state_calls) == 2
    assert observed_delta_fields == expected_delta_fields
    assert final_working_state == {
        "schema_version": "run-working-state-v2",
        "constraints": [],
        "decisions": ["README.md was inspected"],
        "next_steps": [],
    }

    print_section(
        "A1. Run Log 保存的是 Fact",
        {
            "events_path": f".pico/runs/{run_id}/events.jsonl",
            "event_sequence": [
                {
                    "sequence": event.sequence,
                    "kind": event.kind,
                    "call_id": event.call_id,
                }
                for event in events
            ],
            "one_tool_result": {
                "payload_keys": sorted(raw_result["payload"]),
                "outcome_keys": sorted(raw_result["payload"]["outcome"]),
                "tool_call_id_owner": "payload.outcome.tool_call_id",
                "tool_name_owner": "payload.outcome.tool_name",
                "artifact_reference": raw_result["payload"]["outcome"][
                    "artifact_id"
                ],
            },
        },
    )
    print_section(
        "A2. Live、load_run 与 replay 得到同一 Projection",
        {
            "all_summaries_equal": True,
            "run_cursor": replayed.last_cursor.to_dict(),
            "task_contract": replayed.task.contract.to_dict(),
            "working_state_updates": [
                {
                    "call_id": event.call_id,
                    "args": event.args,
                }
                for event in working_state_calls
            ],
            "six_delta_fields_covered": sorted(observed_delta_fields),
            "final_working_state": final_working_state,
            "metrics": replayed.metrics.to_dict(),
            "final_diff": replayed.final_diff.to_dict(),
            "explanation": (
                "JSONL Event 是 Fact；Task、Evidence、Metrics、Pending 与 Final Diff "
                "都是 RunProjection 从这些 Fact 归约出的 View"
            ),
        },
    )
    return agent, events, outcome


def pending_prefix_experiment(events):
    """Replay three legal prefixes to make the one Pending Call visible."""

    call_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "assistant_tool_call" and event.name == "read_file"
    )
    call_id = events[call_index].call_id
    started_index = next(
        index
        for index in range(call_index + 1, len(events))
        if events[index].kind == "tool_started"
        and events[index].call_id == call_id
    )
    result_index = next(
        index
        for index in range(started_index + 1, len(events))
        if events[index].kind == "tool_result"
        and events[index].call_id == call_id
    )

    after_call = replay_events(events[: call_index + 1])
    after_started = replay_events(events[: started_index + 1])
    after_result = replay_events(events[: result_index + 1])
    assert after_call.pending_call_id == call_id
    assert after_started.pending_call_id == call_id
    assert after_result.pending_call_id is None

    print_section(
        "B1. 合法 Event 前缀中的单一 Pending Call",
        [
            {
                "last_fact": "assistant_tool_call",
                "pending_call_id": after_call.pending_call_id,
            },
            {
                "last_fact": "tool_started",
                "pending_call_id": after_started.pending_call_id,
            },
            {
                "last_fact": "tool_result",
                "pending_call_id": after_result.pending_call_id,
            },
        ],
    )


def batch_prefix_experiment(root):
    """Show one durable batch reducing from two pending calls to none."""

    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "b.txt").write_text("beta\n", encoding="utf-8")
    agent = build_agent(root, FakeModelClient([]))
    RunLifecycle(agent).initialize("Read two files")
    calls = (
        ToolCall("read_file", {"path": "a.txt"}, "call_batch_a"),
        ToolCall("read_file", {"path": "b.txt"}, "call_batch_b"),
    )
    agent.apply_run_event(agent.run.run_log.append_tool_batch(calls))
    after_batch = tuple(agent.run.projection.pending_call_ids)
    for call in calls:
        agent.apply_run_event(
            agent.run.run_log.append_tool_started(
                call,
                effect_scope="none",
                potential_effects=[],
            )
        )
    for call in calls:
        agent.apply_run_event(
            agent.run.run_log.append_tool_result(
                ToolOutcome(
                    call.call_id,
                    call.name,
                    "success",
                    "completed",
                    "none",
                    "observed",
                )
            )
        )
    assert after_batch == ("call_batch_a", "call_batch_b")
    assert agent.run.projection.pending_call_ids == ()

    print_section(
        "B2. Observation Batch 是一个有序 Pending 事务",
        {
            "batch_pending_call_ids": list(after_batch),
            "started_order": [call.call_id for call in calls],
            "result_order": [call.call_id for call in calls],
            "pending_after_results": list(agent.run.projection.pending_call_ids),
        },
    )


def interrupted_read_experiment(root):
    """Restart after tool_started and reconcile without replaying the Runner."""

    (root / "README.md").write_text("# Recovery demo\n", encoding="utf-8")
    original = build_agent(root, FakeModelClient([]))
    RunLifecycle(original).initialize(
        "Read README.md after restart",
    )
    run_id = original.run.projection.run_id
    call = ToolCall(
        "read_file",
        {"path": "README.md", "start_line": 1, "end_line": 20},
        "call_interrupted_read",
    )
    original.apply_run_event(original.run.run_log.append_tool_call(call))
    original.apply_run_event(
        original.run.run_log.append_tool_started(
            call,
            effect_scope="none",
            potential_effects=[],
        )
    )
    session_id = original.session.id
    loaded_session = SessionStore(root / ".pico" / "sessions").load(session_id)

    # Creating a second Pico simulates a new process. Its constructor loads the
    # unfinished Run but does not execute or reconcile the tool yet.
    resumed = build_agent(
        root,
        FakeModelClient(
            [
                ModelAction.tool(
                    "read_file",
                    {"path": "README.md", "start_line": 1, "end_line": 20},
                    call_id="call_explicit_retry_read",
                ),
                ModelAction.final("Recovered without blindly replaying read_file."),
            ]
        ),
        session=loaded_session,
    )
    dormant_snapshot = {
        "same_run_id": resumed.run.projection.run_id == run_id,
        "resumable": resumed.run.resumable,
        "pending_call_id": resumed.run.projection.pending_call_id,
    }
    assert dormant_snapshot == {
        "same_run_id": True,
        "resumable": True,
        "pending_call_id": call.call_id,
    }

    outcome = resumed.ask(
        "Continue the same Run"
    )
    assert isinstance(outcome, RunOutcome)
    assert outcome.run_id == run_id
    assert outcome.status == "completed"

    events, projection = resumed.dependencies.run_store.load_run(run_id)
    recovered_result = next(
        event
        for event in events
        if event.kind == "tool_result"
        and event.call_id == call.call_id
    )
    started_count = sum(
        event.kind == "tool_started" and event.call_id == call.call_id
        for event in events
    )
    result_position = events.index(recovered_result)
    guidance_position = next(
        index for index, event in enumerate(events) if event.kind == "user_guidance"
    )
    resumed_position = next(
        index for index, event in enumerate(events) if event.kind == "run_resumed"
    )
    assert recovered_result.payload["recovered_from_interruption"] is True
    assert recovered_result.payload["outcome"]["side_effect_state"] == "none"
    assert started_count == 1
    assert result_position < guidance_position < resumed_position
    assert projection.pending_call_id is None

    print_section(
        "C. 新进程加载 dormant Run，再自动对账",
        {
            "after_Pico_constructor": dormant_snapshot,
            "during_next_ask": {
                "recovered_tool_result": {
                    "status": recovered_result.payload["outcome"]["status"],
                    "execution_state": recovered_result.payload["outcome"][
                        "execution_state"
                    ],
                    "side_effect_state": recovered_result.payload["outcome"][
                        "side_effect_state"
                    ],
                    "recovered_from_interruption": True,
                },
                "tool_started_count": started_count,
                "runner_was_blindly_replayed": False,
                "model_later_requested_a_new_call": "call_explicit_retry_read",
                "event_order": (
                    "recovered tool_result -> user_guidance -> run_resumed"
                ),
            },
            "run_outcome": outcome.to_dict(),
        },
    )


def main():
    with tempfile.TemporaryDirectory(prefix="pico-day2-") as directory:
        root = Path(directory)
        normal_root = root / "fact-projection"
        recovery_root = root / "interrupted-read"
        batch_root = root / "observation-batch"
        normal_root.mkdir()
        recovery_root.mkdir()
        batch_root.mkdir()

        _agent, events, _outcome = fact_projection_experiment(normal_root)
        pending_prefix_experiment(events)
        batch_prefix_experiment(batch_root)
        interrupted_read_experiment(recovery_root)


if __name__ == "__main__":
    main()
