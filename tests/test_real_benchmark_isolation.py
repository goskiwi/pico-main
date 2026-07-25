import json
from pathlib import Path

from evaluation.real_benchmark_evidence import _workspace_isolation_audit


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
    output_dir = run_dir / "refs"
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
                "args": {"files": [{"path": str(outer_repo / "settings.py")}]},
            },
        ),
    )
    output_dir = run_dir / "refs"
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
