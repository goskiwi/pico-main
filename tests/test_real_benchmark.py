import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evaluation.real_benchmark import (
    _attempt_trace_metrics,
    _evaluate_delegate_evidence,
    _failure_category,
    _trace_events,
    _trace_metrics,
    _workspace_isolation_audit,
    build_real_model_client,
    compare_real_benchmark_artifacts,
    load_real_benchmark,
    RealWorldBenchmarkRunner,
    render_real_benchmark_markdown,
    summarize_real_rows,
    VARIANT_FULL,
    validate_real_benchmark,
)
from pico import FakeModelClient
from tests.helpers import UnitTestSandbox


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DELEGATE_BENCHMARK_PATH = PROJECT_ROOT / "benchmarks" / "real_world_tasks_delegate.json"


def test_delegate_live_regression_requires_delegate_many():
    benchmark = load_real_benchmark(DELEGATE_BENCHMARK_PATH, PROJECT_ROOT)

    assert benchmark["name"] == "pico-delegate-live-regression"
    assert len(benchmark["tasks"]) == 1
    task = benchmark["tasks"][0]
    assert task["required_tools"] == ["delegate_many"]
    assert task["require_successful_delegates"] is True
    assert task["expected_delegate_runs"] == 2
    assert task["expected_delegate_attempts"] == 1
    assert "delegate_many" in task["allowed_tools"]


def test_real_benchmark_client_uses_only_the_supplied_workspace_env():
    env = {
        "OPENAI_API_BASE": "https://env.example/v1",
        "OPENAI_API_KEY": "file-key",
    }

    with patch("evaluation.real_benchmark.OpenAICompatibleModelClient") as client_class:
        client = build_real_model_client("file-model", env=env)

    assert client is client_class.return_value
    assert client_class.call_args.kwargs["model"] == "file-model"
    assert client_class.call_args.kwargs["base_url"] == "https://env.example/v1"
    assert client_class.call_args.kwargs["api_key"] == "file-key"


def test_real_benchmark_runner_rejects_unsupported_provider(tmp_path):
    with pytest.raises(ValueError, match="provider must be 'openai'"):
        RealWorldBenchmarkRunner(
            benchmark_path=DELEGATE_BENCHMARK_PATH,
            artifact_path=tmp_path / "artifact.json",
            report_path=tmp_path / "report.md",
            workspace_root=tmp_path / "workspaces",
            provider="anthropic",
        )


def test_benchmark_rejects_required_tools_not_in_the_allowed_set():
    payload = json.loads(DELEGATE_BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["tasks"][0]["allowed_tools"].remove("delegate_many")

    with pytest.raises(ValueError, match="required_tools are not allowed"):
        validate_real_benchmark(payload, PROJECT_ROOT)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_delegate_runs", True, "must be an integer"),
        ("expected_delegate_runs", 0, "must be positive"),
        ("expected_delegate_attempts", "1", "must be an integer"),
        ("expected_delegate_attempts", 0, "must be positive"),
    ],
)
def test_benchmark_rejects_invalid_delegate_evidence_expectations(
    field, value, message
):
    payload = json.loads(DELEGATE_BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["tasks"][0][field] = value

    with pytest.raises(ValueError, match=message):
        validate_real_benchmark(payload, PROJECT_ROOT)


def test_benchmark_delegate_expectations_require_successful_delegates():
    payload = json.loads(DELEGATE_BENCHMARK_PATH.read_text(encoding="utf-8"))
    payload["tasks"][0]["require_successful_delegates"] = False

    with pytest.raises(ValueError, match="requires require_successful_delegates=true"):
        validate_real_benchmark(payload, PROJECT_ROOT)


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
    trace_path.write_text(
        '{"event":"run_started"}\n{"event":\n[]\n', encoding="utf-8"
    )

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


def _write_run_trace(run_dir, workspace_root, *, finished=True, tool_events=()):
    run_dir.mkdir(parents=True)
    events = [
        {
            "event": "run_started",
            "workspace_root": str(Path(workspace_root).resolve()),
        },
        *tool_events,
    ]
    if finished:
        events.append({"event": "run_finished"})
    (run_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_workspace_isolation_audit_accepts_finished_runs_inside_workspace(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    run_dir = tmp_path / "runs" / "run_child"
    _write_run_trace(
        run_dir,
        workspace_root,
        tool_events=(
            {
                "event": "tool_executed",
                "name": "search",
                "args": {"path": ".", "pattern": "normalize_label"},
            },
        ),
    )
    output_dir = run_dir / "tool_outputs"
    output_dir.mkdir()
    (output_dir / "0001_search.txt").write_text(
        f"{workspace_root / 'settings.py'}:1:def normalize_label(label):\n",
        encoding="utf-8",
    )

    audit = _workspace_isolation_audit(
        workspace_root,
        [run_dir],
        {"verifier_files": [{"source": "verifiers/test_hidden.py"}]},
    )

    assert audit["ok"] is True
    assert audit["run_ids"] == ["run_child"]
    assert audit["violations"] == []


def test_workspace_isolation_audit_rejects_root_search_and_verifier_leaks(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    outer_repo = tmp_path / "outer"
    outer_repo.mkdir()
    run_dir = tmp_path / "runs" / "run_child"
    _write_run_trace(
        run_dir,
        outer_repo,
        finished=False,
        tool_events=(
            {
                "event": "tool_executed",
                "name": "read_file",
                "args": {"path": str(outer_repo / "settings.py")},
            },
        ),
    )
    output_dir = run_dir / "tool_outputs"
    output_dir.mkdir()
    (output_dir / "0001_search.txt").write_text(
        "\n".join(
            [
                f"{outer_repo / 'settings.py'}:1:def normalize_label(label):",
                f"{outer_repo}/verifiers/test_hidden.py:7:def test_hidden():",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    audit = _workspace_isolation_audit(
        workspace_root,
        [run_dir],
        {"verifier_files": [{"source": "verifiers/test_hidden.py"}]},
    )

    violation_types = {item["type"] for item in audit["violations"]}
    assert audit["ok"] is False
    assert violation_types == {
        "workspace_root_mismatch",
        "unfinished_run",
        "tool_path_outside_workspace",
        "search_result_outside_workspace",
        "verifier_source_exposed",
    }


def test_workspace_isolation_audit_rejects_missing_run_evidence(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    audit = _workspace_isolation_audit(
        workspace_root,
        [],
        {"verifier_files": []},
    )

    assert audit["ok"] is False
    assert audit["violations"] == [{"type": "missing_runs", "run_id": ""}]


def test_workspace_isolation_audit_rejects_truncated_trace_jsonl(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    run_dir = tmp_path / "runs" / "run_main"
    run_dir.mkdir(parents=True)
    (run_dir / "trace.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "run_started",
                        "workspace_root": str(workspace_root.resolve()),
                    }
                ),
                '{"event":"tool_executed"',
                json.dumps({"event": "run_finished"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    audit = _workspace_isolation_audit(
        workspace_root, [run_dir], {"verifier_files": []}
    )

    assert audit["ok"] is False
    invalid = [item for item in audit["violations"] if item["type"] == "invalid_trace"]
    assert len(invalid) == 1
    assert invalid[0]["run_id"] == "run_main"
    assert invalid[0]["line_number"] == 2


def test_workspace_isolation_failure_has_highest_failure_priority():
    task_state = SimpleNamespace(status="completed", stop_reason="")
    verifier_result = SimpleNamespace(timed_out=False, returncode=0)

    category = _failure_category(
        task_state,
        verifier_result,
        {"summary": {"changed_files": ["settings.py"]}},
        workspace_isolation_violations=[{"type": "workspace_root_mismatch"}],
        missing_required_tools=["delegate_many"],
    )

    assert category == "workspace_isolation_failed"


def test_run_task_skips_hidden_verifier_when_isolation_audit_fails(
    tmp_path,
):
    runner = RealWorldBenchmarkRunner(
        benchmark_path=DELEGATE_BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(
        [
            '<tool>{"name":"patch_file","args":{"path":"settings.py","old_text":"    return label\\n","new_text":"    return label.strip().upper()\\n"}}</tool>',
            "<final>Updated the implementation.</final>",
        ]
    )
    runner._sandbox = lambda workspace_root: UnitTestSandbox(workspace_root)
    task = load_real_benchmark(DELEGATE_BENCHMARK_PATH, PROJECT_ROOT)["tasks"][0]
    failed_audit = {
        "ok": False,
        "expected_workspace_root": "fixture",
        "run_count": 1,
        "run_ids": ["run_main"],
        "violations": [{"type": "workspace_root_mismatch", "run_id": "run_main"}],
    }

    with (
        patch(
            "evaluation.real_benchmark._workspace_isolation_audit",
            return_value=failed_audit,
        ),
        patch.object(runner, "_verify") as verify,
    ):
        row = runner.run_task(task, variant=VARIANT_FULL, repetition=1)

    verify.assert_not_called()
    assert row["passed"] is False
    assert row["failure_category"] == "workspace_isolation_failed"
    assert row["workspace_isolation"] == failed_audit
    assert row["verifier"]["skipped"] is True
    assert row["verifier"]["exit_code"] == 125
    assert row["parent_model_calls"] == 2
    assert row["delegate_model_calls"] == 0
    assert row["total_model_calls"] == 2
    assert row["model_calls"] == row["total_model_calls"]
    assert row["input_tokens"] == row["total_input_tokens"]
    assert row["output_tokens"] == row["total_output_tokens"]
    assert row["cached_tokens"] == row["total_cached_tokens"]
    assert row["model_failures"] == row["total_model_failures"] == 0
    assert row["model_action_rejections"] == row["total_model_action_rejections"]
    assert row["action_protocols"] == row["total_action_protocols"]
    assert row["parent_action_protocols"] == row["action_protocols"]
    assert row["delegate_action_protocols"] == []
    assert row["delegate_run_count"] == 0


def test_run_task_fails_closed_when_trace_metrics_report_parse_error(tmp_path):
    benchmark_path = PROJECT_ROOT / "benchmarks" / "real_world_tasks.json"
    runner = RealWorldBenchmarkRunner(
        benchmark_path=benchmark_path,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(
        [
            '<tool>{"name":"patch_file","args":{"path":"inventory.py","old_text":"    return cleaned\\n","new_text":"    return cleaned.upper()\\n"}}</tool>',
            "<final>Updated the implementation.</final>",
        ]
    )
    runner._sandbox = lambda workspace_root: UnitTestSandbox(workspace_root)
    task = load_real_benchmark(benchmark_path, PROJECT_ROOT)["tasks"][0]

    def metrics_with_parse_error(*args, **kwargs):
        metrics = _attempt_trace_metrics(*args, **kwargs)
        error = {
            "run_id": Path(args[0]).name,
            "line_number": 99,
            "error": "truncated JSON",
        }
        metrics["parent"]["trace_parse_errors"] = [error]
        metrics["total"]["trace_parse_errors"] = [error]
        return metrics

    clean_audit = {
        "ok": True,
        "expected_workspace_root": "fixture",
        "run_count": 1,
        "run_ids": ["run_main"],
        "violations": [],
    }
    with (
        patch(
            "evaluation.real_benchmark._attempt_trace_metrics",
            side_effect=metrics_with_parse_error,
        ),
        patch(
            "evaluation.real_benchmark._workspace_isolation_audit",
            return_value=clean_audit,
        ),
        patch.object(
            runner,
            "_verify",
            return_value=SimpleNamespace(
                returncode=0, timed_out=False, stdout="", stderr=""
            ),
        ),
    ):
        row = runner.run_task(task, variant=VARIANT_FULL, repetition=1)

    assert row["passed"] is False
    assert row["failure_category"] == "trace_parse_error"
    assert row["trace_parse_errors"][0]["line_number"] == 99


def test_run_task_cross_checks_exact_delegate_attempt_and_child_runs(tmp_path):
    runner = RealWorldBenchmarkRunner(
        benchmark_path=DELEGATE_BENCHMARK_PATH,
        artifact_path=tmp_path / "artifact.json",
        report_path=tmp_path / "report.md",
        workspace_root=tmp_path / "workspaces",
    )
    runner._shared_model_client = FakeModelClient(
        [
            '<tool>{"name":"delegate_many","args":{"tasks":[{"role":"explore","task":"inspect settings.py","max_steps":2},{"role":"review","task":"inspect tests","max_steps":2}]}}</tool>',
            "<final>Explorer completed.</final>",
            "<final>Reviewer completed.</final>",
            '<tool>{"name":"patch_file","args":{"path":"settings.py","old_text":"    return label\\n","new_text":"    cleaned = label.strip()\\n    if not cleaned:\\n        raise ValueError(\\"label must not be empty\\")\\n    return cleaned.upper()\\n"}}</tool>',
            "<final>Updated and verified.</final>",
        ]
    )
    runner._sandbox = lambda workspace_root: UnitTestSandbox(workspace_root)
    task = load_real_benchmark(DELEGATE_BENCHMARK_PATH, PROJECT_ROOT)["tasks"][0]

    with patch.object(
        runner,
        "_verify",
        return_value=SimpleNamespace(
            returncode=0, timed_out=False, stdout="4 passed", stderr=""
        ),
    ):
        row = runner.run_task(task, variant=VARIANT_FULL, repetition=1)

    assert row["passed"] is True
    assert row["delegate_run_count"] == 2
    assert len(row["delegate_agent_ids"]) == 2
    assert row["delegate_evidence"]["ok"] is True
    assert row["delegate_evidence"]["attempt_count"] == 1
    assert row["delegate_evidence"]["requested_count"] == 2
    assert row["delegate_evidence"]["completed_count"] == 2
    assert row["delegate_evidence"]["reported_agent_ids"] == row[
        "delegate_evidence"
    ]["related_agent_ids"]


def _benchmark_row(**updates):
    row = {
        "task_id": "task-one",
        "category": "regression",
        "variant": VARIANT_FULL,
        "repetition": 1,
        "passed": True,
        "failure_category": "",
        "tool_steps": 1,
        "model_calls": 1,
        "model_action_rejections": 0,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_tokens": 3,
        "agent_duration_ms": 100,
        "total_duration_ms": 120,
        "action_protocols": ["responses_api"],
    }
    row.update(updates)
    return row


def _benchmark_artifact(row, *, schema_version):
    return {
        "schema_version": schema_version,
        "captured_at": "2026-07-22T00:00:00+00:00",
        "execution_mode": "live_llm",
        "provider": "openai",
        "model": "test-model",
        "runtime": {"commit_sha": "abc123", "working_tree_dirty": False},
        "benchmark": {
            "name": "test benchmark",
            "task_count": 1,
            "fixture_snapshot_id": "sha256:fixture",
            "evaluation_snapshot_id": "sha256:evaluation",
        },
        "run_config": {},
        "sandbox": {
            "image": "test-image",
            "cpus": 1,
            "memory": "1g",
            "pids_limit": 32,
        },
        "repetitions": 1,
        "summary": summarize_real_rows([row]),
        "rows": [row],
    }


def test_summary_and_report_expose_parent_delegate_total_costs():
    row = _benchmark_row(
        parent_model_calls=1,
        delegate_model_calls=2,
        total_model_calls=3,
        model_calls=3,
        parent_input_tokens=10,
        delegate_input_tokens=20,
        total_input_tokens=30,
        input_tokens=30,
        parent_output_tokens=2,
        delegate_output_tokens=4,
        total_output_tokens=6,
        output_tokens=6,
        parent_cached_tokens=3,
        delegate_cached_tokens=5,
        total_cached_tokens=8,
        cached_tokens=8,
        parent_model_duration_ms=100,
        delegate_model_duration_ms=250,
        total_model_duration_ms=350,
        parent_model_failures=1,
        delegate_model_failures=2,
        total_model_failures=3,
        model_failures=3,
        parent_model_action_rejections=1,
        delegate_model_action_rejections=2,
        total_model_action_rejections=3,
        model_action_rejections=3,
        parent_action_protocols=["parent_protocol"],
        delegate_action_protocols=["child_protocol"],
        total_action_protocols=["child_protocol", "parent_protocol"],
        action_protocols=["child_protocol", "parent_protocol"],
        delegate_run_count=2,
    )
    artifact = _benchmark_artifact(row, schema_version=3)
    artifact["run_config"]["model_cost_scope"] = "attempt_parent_and_related_delegates"

    metrics = artifact["summary"]["variants"][VARIANT_FULL]
    report = render_real_benchmark_markdown(artifact)

    assert metrics["avg_parent_model_calls"] == 1
    assert metrics["avg_delegate_model_calls"] == 2
    assert metrics["avg_total_model_calls"] == 3
    assert metrics["avg_model_calls"] == 3
    assert metrics["total_parent_input_tokens"] == 10
    assert metrics["total_delegate_input_tokens"] == 20
    assert metrics["total_input_tokens"] == 30
    assert metrics["delegate_run_count"] == 2
    assert metrics["avg_delegate_run_count"] == 2
    assert metrics["total_delegate_run_count"] == 2
    assert metrics["avg_parent_model_failures"] == 1
    assert metrics["avg_delegate_model_failures"] == 2
    assert metrics["avg_total_model_failures"] == 3
    assert metrics["avg_model_failures"] == 3
    assert metrics["avg_parent_model_action_rejections"] == 1
    assert metrics["avg_delegate_model_action_rejections"] == 2
    assert metrics["avg_total_model_action_rejections"] == 3
    assert metrics["avg_model_action_rejections"] == 3
    assert metrics["parent_action_protocols"] == ["parent_protocol"]
    assert metrics["delegate_action_protocols"] == ["child_protocol"]
    assert metrics["action_protocols"] == ["child_protocol", "parent_protocol"]
    assert "Avg calls P/D/T" in report
    assert "Avg delegates" in report
    assert "Avg failures P/D/T" in report
    assert "Avg rejects P/D/T" in report
    assert "child_protocol, parent_protocol" in report
    assert "1.00/2.00/3.00" in report
    assert "10/20/30" in report
    assert "0.10s/0.25s/0.35s" in report


def test_report_keeps_schema_v2_parent_only_artifacts_renderable():
    artifact = _benchmark_artifact(_benchmark_row(), schema_version=2)

    report = render_real_benchmark_markdown(artifact)
    comparison = compare_real_benchmark_artifacts(artifact, artifact)

    assert "Model cost scope: `parent_run_only`" in report
    assert "1.00/0.00/1.00" in report
    assert "10/0/10" in report
    assert comparison["model_cost_scope"] == "parent_run_only"
    assert comparison["summary"]["baseline_avg_model_calls"] == 1


def test_comparison_rejects_mixed_parent_only_and_attempt_total_cost_scopes():
    row = _benchmark_row()
    baseline = _benchmark_artifact(row, schema_version=2)
    candidate = _benchmark_artifact(row, schema_version=3)

    with pytest.raises(ValueError, match="model-cost scopes do not match"):
        compare_real_benchmark_artifacts(baseline, candidate)
