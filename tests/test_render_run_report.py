import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from render_run_report import main, render_run  # noqa: E402


def write_run(run_dir):
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "task_id": "task_001",
                "status": "completed",
                "stop_reason": "final_answer_returned",
                "final_answer": "Done.",
                "attempts": 2,
                "tool_steps": 1,
                "dry_run": False,
                "summary": {
                    "task": "Inspect README",
                    "changed_files": ["README.md"],
                    "failed_tools": [],
                    "security_events": [],
                },
                "tool_audit": [
                    {
                        "name": "read_file",
                        "status": "ok",
                        "capability": "read",
                        "duration_ms": 3,
                        "path": "README.md",
                        "affected_paths": [],
                        "approval_decision": "not_required",
                        "shell_policy_reason": "",
                    }
                ],
                "prompt_metadata": {
                    "prompt_chars": 1234,
                    "prompt_estimated_tokens": 309,
                    "input_tokens": 320,
                    "output_tokens": 24,
                    "cached_tokens": 0,
                    "cache_hit": False,
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "task_state.json").write_text(
        json.dumps({"run_id": run_dir.name, "status": "completed", "user_request": "Inspect README"}) + "\n",
        encoding="utf-8",
    )
    (run_dir / "trace.jsonl").write_text(
        json.dumps({"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"}) + "\n"
        + json.dumps({"event": "tool_executed", "name": "read_file", "created_at": "2026-04-07T00:00:01+00:00"}) + "\n"
        + json.dumps({"event": "run_finished", "run_duration_ms": 42, "created_at": "2026-04-07T00:00:02+00:00"}) + "\n",
        encoding="utf-8",
    )


def test_render_run_report_writes_single_html(tmp_path):
    run_dir = tmp_path / ".pico" / "runs" / "run_001"
    write_run(run_dir)

    output = render_run(run_dir)
    html = output.read_text(encoding="utf-8")

    assert output == run_dir / "report.html"
    assert "pico Run Report" in html
    assert "Inspect README" in html
    assert "read_file" in html
    assert "README.md" in html
    assert "Trace Events" in html
    assert "Prompt chars" in html


def test_render_run_report_cli_all_writes_index(tmp_path, capsys):
    runs_root = tmp_path / ".pico" / "runs"
    write_run(runs_root / "run_001")
    write_run(runs_root / "run_002")

    assert main([str(runs_root), "--all"]) == 0

    captured = capsys.readouterr()
    index = runs_root / "index.html"
    assert str(index) in captured.out
    assert index.exists()
    assert (runs_root / "run_001" / "report.html").exists()
    assert (runs_root / "run_002" / "report.html").exists()
    assert "run_001" in index.read_text(encoding="utf-8")

