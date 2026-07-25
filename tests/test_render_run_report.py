import importlib.util
import json
from pathlib import Path


def _renderer_module():
    path = Path(__file__).parents[1] / "scripts" / "render_run_report.py"
    spec = importlib.util.spec_from_file_location("render_run_report", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_static_report_renders_active_canvas_and_archived_phases(tmp_path):
    run_dir = tmp_path / "run_001"
    phases = run_dir / "phases"
    phases.mkdir(parents=True)
    (run_dir / "task.mmd").write_text(
        'flowchart TD\n  G["goal | running | inspect"]\n', encoding="utf-8"
    )
    (phases / "phase_001.mmd").write_text(
        'flowchart TD\n  P["phase_001 | done | 2 archived task steps"]\n',
        encoding="utf-8",
    )
    (phases / "index.json").write_text(
        json.dumps(
            [
                {
                    "phase_id": "phase_001",
                    "path": "phases/phase_001.mmd",
                    "status": "done",
                    "node_count": 2,
                }
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "task_canvas_path": str(run_dir / "task.mmd"),
                "phase_index_path": str(phases / "index.json"),
                "prompt_metadata": {"prompt_tokens": 12},
            }
        ),
        encoding="utf-8",
    )

    html = _renderer_module().render_html(run_dir)

    assert 'class="mermaid"' in html
    assert "mermaid@11" in html
    assert "Archived phases (1)" in html
    assert "phase_001" in html
