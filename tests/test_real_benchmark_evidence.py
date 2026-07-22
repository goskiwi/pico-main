import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.real_benchmark_evidence import (
    _attempt_trace_metrics,
    _evaluate_delegate_evidence,
    _failure_category,
    _trace_events,
    _trace_metrics,
)


def test_trace_metrics_records_executed_tools(tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(
            [
                json.dumps({"event": "model_requested"}),
                json.dumps(
                    {
                        "event": "model_parsed",
                        "duration_ms": 17,
                        "completion_metadata": {
                            "input_tokens": 11,
                            "output_tokens": 3,
                            "cached_tokens": 5,
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "tool_executed",
                        "name": "delegate_many",
                        "tool_status": "error",
                        "result": "x" * 500,
                        "delegate_outcome": {
                            "requested_count": 1,
                            "completed_count": 0,
                            "failed_count": 1,
                            "items": [
                                {
                                    "index": 1,
                                    "role": "explore",
                                    "status": "not_run",
                                    "agent_id": "",
                                }
                            ],
                        },
                    }
                ),
                json.dumps({"event": "tool_executed", "name": "read_file"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    metrics = _trace_metrics(trace_path)

    assert metrics["model_calls"] == 1
    assert metrics["model_duration_ms"] == 17
    assert metrics["input_tokens"] == 11
    assert metrics["output_tokens"] == 3
    assert metrics["cached_tokens"] == 5
    assert metrics["executed_tools"] == ["delegate_many", "read_file"]
    assert metrics["failed_delegate_outcomes"] == ["delegate_many"]


def _successful_delegate_attempt(*, agent_ids=("child-1", "child-2")):
    return {
        "name": "delegate_many",
        "tool_status": "ok",
        "requested_count": len(agent_ids),
        "completed_count": len(agent_ids),
        "failed_count": 0,
        "items": [
            {
                "index": index,
                "role": "explore" if index == 1 else "review",
                "status": "ok",
                "agent_id": agent_id,
                "child_status": "completed",
                "stop_reason": "final_answer_returned",
            }
            for index, agent_id in enumerate(agent_ids, start=1)
        ],
        "issues": [],
        "successful": True,
    }


def test_trace_metrics_uses_structured_delegate_evidence_not_truncated_result(tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    outcome = {
        "requested_count": 2,
        "completed_count": 2,
        "failed_count": 0,
        "items": _successful_delegate_attempt()["items"],
    }
    trace_path.write_text(
        json.dumps(
            {
                "event": "tool_executed",
                "name": "delegate_many",
                "tool_status": "ok",
                "result": ("very long child answer " * 100) + " status=error",
                "delegate_outcome": outcome,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    metrics = _trace_metrics(trace_path)

    assert metrics["failed_delegate_outcomes"] == []
    assert metrics["delegate_attempts"][0]["successful"] is True


def test_trace_parser_structures_invalid_or_non_object_jsonl_records(tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text('{"event":"run_started"}\n{"event":\n[]\n', encoding="utf-8")

    events = _trace_events(trace_path)
    metrics = _trace_metrics(trace_path)

    parse_errors = [event for event in events if event["event"] == "trace_parse_error"]
    assert [error["line_number"] for error in parse_errors] == [2, 3]
    assert len(metrics["trace_parse_errors"]) == 2


def test_trace_parser_structures_invalid_utf8(tmp_path):
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_bytes(b'\xff{"event":"run_started"}\n')

    events = _trace_events(trace_path)

    assert events[0]["event"] == "trace_parse_error"
    assert events[0]["line_number"] == 0
    assert "not valid UTF-8" in events[0]["error"]


def test_delegate_evidence_accepts_one_two_child_attempt_with_matching_runs():
    evidence = _evaluate_delegate_evidence(
        {"delegate_attempts": [_successful_delegate_attempt()]},
        delegate_run_count=2,
        delegate_agent_ids=["child-2", "child-1"],
        required=True,
        expected_delegate_runs=2,
        expected_delegate_attempts=1,
    )

    assert evidence["ok"] is True
    assert evidence["attempt_count"] == 1
    assert evidence["requested_count"] == 2
    assert evidence["completed_count"] == 2
    assert evidence["reported_agent_ids"] == evidence["related_agent_ids"]


@pytest.mark.parametrize(
    ("attempts", "run_count", "agent_ids"),
    [
        (
            [
                {
                    **_successful_delegate_attempt(agent_ids=("child-1",)),
                    "tool_status": "rejected",
                    "completed_count": 0,
                    "failed_count": 1,
                    "issues": ["tool_status:rejected"],
                    "successful": False,
                }
            ],
            0,
            [],
        ),
        (
            [
                {
                    **_successful_delegate_attempt(agent_ids=("child-1",)),
                    "tool_status": "error",
                    "completed_count": 0,
                    "failed_count": 1,
                    "issues": ["tool_status:error"],
                    "successful": False,
                }
            ],
            0,
            [],
        ),
        (
            [
                {
                    "name": "delegate_many",
                    "tool_status": "rejected",
                    "requested_count": 0,
                    "completed_count": 0,
                    "failed_count": 0,
                    "items": [],
                    "issues": ["no_children_requested"],
                    "successful": False,
                }
            ],
            0,
            [],
        ),
        (
            [
                {
                    **_successful_delegate_attempt(agent_ids=("child-1",)),
                    "requested_count": 2,
                    "completed_count": 1,
                    "failed_count": 0,
                    "issues": ["requested_item_count_mismatch"],
                    "successful": False,
                }
            ],
            1,
            ["child-1"],
        ),
        (
            [
                {
                    **_successful_delegate_attempt(agent_ids=("child-1",)),
                    "requested_count": 2,
                    "completed_count": 1,
                    "failed_count": 1,
                    "issues": ["child_not_completed:2"],
                    "successful": False,
                }
            ],
            1,
            ["child-1"],
        ),
        ([_successful_delegate_attempt()], 0, []),
    ],
    ids=[
        "argument-rejected",
        "tool-exception",
        "zero-child",
        "missing-child",
        "failed-child",
        "zero-related-runs",
    ],
)
def test_required_delegate_evidence_fails_closed(attempts, run_count, agent_ids):
    evidence = _evaluate_delegate_evidence(
        {"delegate_attempts": attempts},
        delegate_run_count=run_count,
        delegate_agent_ids=agent_ids,
        required=True,
        expected_delegate_runs=2,
        expected_delegate_attempts=1,
    )

    assert evidence["ok"] is False
    assert evidence["issues"]


def test_delegate_manifest_rejects_two_separate_one_child_attempts():
    attempts = [
        _successful_delegate_attempt(agent_ids=("child-1",)),
        _successful_delegate_attempt(agent_ids=("child-2",)),
    ]

    evidence = _evaluate_delegate_evidence(
        {"delegate_attempts": attempts},
        delegate_run_count=2,
        delegate_agent_ids=["child-1", "child-2"],
        required=True,
        expected_delegate_runs=2,
        expected_delegate_attempts=1,
    )

    assert evidence["ok"] is False
    assert "expected_delegate_attempt_count_mismatch" in evidence["issues"]


def test_ordinary_task_ignores_legacy_delegate_trace_without_metadata():
    evidence = _evaluate_delegate_evidence(
        {
            "delegate_attempts": [
                {
                    "name": "delegate",
                    "issues": ["missing_delegate_outcome"],
                    "successful": False,
                }
            ]
        },
        delegate_run_count=0,
        delegate_agent_ids=[],
        required=False,
    )

    assert evidence["ok"] is True
    assert evidence["required"] is False


def _write_cost_trace(
    run_dir,
    workspace_root,
    *,
    agent_id,
    parent_agent_id="",
    depth=0,
    usages=(),
    action_protocol="responses_function",
    failure_durations=(),
    rejection_count=0,
):
    run_dir.mkdir(parents=True)
    events = [
        {
            "event": "run_started",
            "agent_id": agent_id,
            "parent_agent_id": parent_agent_id,
            "depth": depth,
            "workspace_root": str(Path(workspace_root).resolve()),
        }
    ]
    for usage in usages:
        events.extend(
            [
                {"event": "model_requested"},
                {
                    "event": "model_parsed",
                    "action_protocol": action_protocol,
                    "duration_ms": usage[3],
                    "completion_metadata": {
                        "input_tokens": usage[0],
                        "output_tokens": usage[1],
                        "cached_tokens": usage[2],
                    },
                },
            ]
        )
    for duration_ms in failure_durations:
        events.extend(
            [
                {"event": "model_requested"},
                {"event": "model_failed", "duration_ms": duration_ms},
            ]
        )
    events.extend({"event": "model_action_rejected"} for _ in range(rejection_count))
    events.append({"event": "run_finished"})
    (run_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_attempt_trace_metrics_aggregates_only_new_related_scoped_runs(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    runs_root = workspace_root / ".pico" / "runs"
    parent = runs_root / "run_parent"
    child_one = runs_root / "run_child_one"
    child_two = runs_root / "run_child_two"
    unrelated = runs_root / "run_unrelated"
    wrong_workspace = runs_root / "run_wrong_workspace"
    historical = runs_root / "run_historical"
    nested = runs_root / "archive" / "run_nested"

    _write_cost_trace(
        parent,
        workspace_root,
        agent_id="agent_parent",
        usages=((10, 2, 4, 100), (20, 3, 0, 200)),
    )
    _write_cost_trace(
        child_one,
        workspace_root,
        agent_id="agent_child_one",
        parent_agent_id="agent_parent",
        depth=1,
        usages=((7, 2, 1, 80),),
    )
    _write_cost_trace(
        child_two,
        workspace_root,
        agent_id="agent_child_two",
        parent_agent_id="agent_parent",
        depth=1,
        usages=((8, 1, 0, 90), (9, 2, 2, 110)),
    )
    _write_cost_trace(
        unrelated,
        workspace_root,
        agent_id="agent_unrelated",
        parent_agent_id="agent_other",
        depth=1,
        usages=((1000, 1000, 1000, 1000),),
    )
    _write_cost_trace(
        wrong_workspace,
        tmp_path / "other-workspace",
        agent_id="agent_wrong_workspace",
        parent_agent_id="agent_parent",
        depth=1,
        usages=((1000, 1000, 1000, 1000),),
    )
    _write_cost_trace(
        historical,
        workspace_root,
        agent_id="agent_historical",
        parent_agent_id="agent_parent",
        depth=1,
        usages=((1000, 1000, 1000, 1000),),
    )
    _write_cost_trace(
        nested,
        workspace_root,
        agent_id="agent_nested",
        parent_agent_id="agent_parent",
        depth=1,
        usages=((1000, 1000, 1000, 1000),),
    )

    metrics = _attempt_trace_metrics(
        parent,
        [parent, child_one, child_two, unrelated, wrong_workspace, nested],
        workspace_root,
    )

    assert metrics["delegate_run_count"] == 2
    assert metrics["delegate_run_ids"] == ["run_child_one", "run_child_two"]
    expected_parent_costs = {
        "model_calls": 2,
        "input_tokens": 30,
        "output_tokens": 5,
        "cached_tokens": 4,
        "model_duration_ms": 300,
    }
    assert {
        key: metrics["parent"][key] for key in expected_parent_costs
    } == expected_parent_costs
    assert metrics["delegate"] == {
        "model_calls": 3,
        "input_tokens": 24,
        "output_tokens": 5,
        "cached_tokens": 3,
        "model_duration_ms": 280,
        "model_failures": 0,
        "model_action_rejections": 0,
        "action_protocols": ["responses_function"],
        "trace_parse_errors": [],
    }
    assert metrics["total"] == {
        "model_calls": 5,
        "input_tokens": 54,
        "output_tokens": 10,
        "cached_tokens": 7,
        "model_duration_ms": 580,
        "model_failures": 0,
        "model_action_rejections": 0,
        "action_protocols": ["responses_function"],
        "trace_parse_errors": [],
    }


def test_attempt_trace_metrics_leave_non_delegate_costs_unchanged(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    parent = workspace_root / ".pico" / "runs" / "run_parent"
    _write_cost_trace(
        parent,
        workspace_root,
        agent_id="agent_parent",
        usages=((13, 5, 2, 41),),
    )

    metrics = _attempt_trace_metrics(parent, [parent], workspace_root)

    assert metrics["delegate_run_count"] == 0
    assert metrics["delegate"] == {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "model_duration_ms": 0,
        "model_failures": 0,
        "model_action_rejections": 0,
        "action_protocols": [],
        "trace_parse_errors": [],
    }
    assert {key: metrics["parent"][key] for key in metrics["total"]} == metrics["total"]


def test_attempt_trace_metrics_include_delegate_failures_rejections_and_protocols(
    tmp_path,
):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    runs_root = workspace_root / ".pico" / "runs"
    parent = runs_root / "run_parent"
    child = runs_root / "run_child"
    _write_cost_trace(
        parent,
        workspace_root,
        agent_id="agent_parent",
        usages=((10, 2, 0, 100),),
        action_protocol="parent_protocol",
        failure_durations=(50,),
        rejection_count=1,
    )
    _write_cost_trace(
        child,
        workspace_root,
        agent_id="agent_child",
        parent_agent_id="agent_parent",
        depth=1,
        usages=((20, 4, 0, 200),),
        action_protocol="child_protocol",
        failure_durations=(60, 70),
        rejection_count=2,
    )

    metrics = _attempt_trace_metrics(parent, [parent, child], workspace_root)

    assert metrics["parent"]["model_failures"] == 1
    assert metrics["delegate"]["model_failures"] == 2
    assert metrics["total"]["model_failures"] == 3
    assert metrics["parent"]["model_action_rejections"] == 1
    assert metrics["delegate"]["model_action_rejections"] == 2
    assert metrics["total"]["model_action_rejections"] == 3
    assert metrics["parent"]["action_protocols"] == ["parent_protocol"]
    assert metrics["delegate"]["action_protocols"] == ["child_protocol"]
    assert metrics["total"]["action_protocols"] == [
        "child_protocol",
        "parent_protocol",
    ]


def test_required_tool_failure_is_reported_before_verifier_status():
    task_state = SimpleNamespace(status="completed", stop_reason="")
    verifier_result = SimpleNamespace(timed_out=False, returncode=0)

    category = _failure_category(
        task_state,
        verifier_result,
        {"summary": {"changed_files": ["settings.py"]}},
        missing_required_tools=["delegate_many"],
    )

    assert category == "required_tool_missing"


def test_delegate_outcome_failure_is_reported_after_required_tool_check():
    task_state = SimpleNamespace(status="completed", stop_reason="")
    verifier_result = SimpleNamespace(timed_out=False, returncode=0)

    category = _failure_category(
        task_state,
        verifier_result,
        {"summary": {"changed_files": ["settings.py"]}},
        failed_delegate_outcomes=["delegate_many"],
    )

    assert category == "delegate_outcome_failed"
