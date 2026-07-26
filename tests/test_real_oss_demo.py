import importlib.util
from pathlib import Path


def _demo_module():
    path = Path(__file__).parents[1] / "scripts" / "run_real_oss_v1_demo.py"
    spec = importlib.util.spec_from_file_location("run_real_oss_v1_demo", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_real_oss_demo_runs_only_the_frozen_click_task(tmp_path):
    demo = _demo_module()
    args = demo.build_arg_parser().parse_args(
        ["--output-dir", str(tmp_path / "demo"), "--model", "test-model"]
    )
    paths = demo.demo_paths(args.output_dir)
    command = demo.build_benchmark_command(args, paths)

    assert "click_empty_bytes_echo" in command
    assert command.count("--task") == 1
    assert command[command.index("--variant") + 1] == "full"
    assert "no_repo_map" not in command
    assert "--require-clean-worktree" not in command
    assert paths["workspace"] == (
        (tmp_path / "demo")
        / "workspaces"
        / "rep-1"
        / "full"
        / "click_empty_bytes_echo"
        / "click_empty_bytes_echo"
    )


def test_real_oss_demo_undo_command_targets_only_the_demo_run(tmp_path):
    demo = _demo_module()
    command = demo._undo_command(tmp_path / "workspace", "run_123", dry_run=True)

    assert command[-1] == "--dry-run"
    assert command[command.index("--cwd") + 1] == str(tmp_path / "workspace")
    assert command[command.index("--run") + 1] == "run_123"
