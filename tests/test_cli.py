import json
from types import SimpleNamespace

import pico.cli as cli


def test_project_skills_require_explicit_cli_trust():
    parser = cli.build_arg_parser()

    assert parser.parse_args([]).trust_project is False
    assert parser.parse_args(["--trust-project"]).trust_project is True


def test_build_status_reports_repl_and_task_boundaries():
    agent = SimpleNamespace(
        model_client=SimpleNamespace(model="gpt-5.6-luna"),
        session_path="/tmp/pico-session.json",
        last_prompt_metadata={"prompt_tokens": 1_234, "prompt_budget_tokens": 12_000},
        _last_prefix_refresh={"workspace_changed": True},
        current_task_state=SimpleNamespace(
            run_id="run_123",
            status="completed",
            context_compactions=[{"sequence": 1}],
        ),
    )

    assert cli.build_status(agent) == "\n".join(
        (
            "model: gpt-5.6-luna",
            "session: /tmp/pico-session.json",
            "workspace refresh for last task: refreshed",
            "prompt: 1234/12000 tokens",
            "task compaction policy: 24000 tokens, max 3 per task",
            "last task: run_123 | completed | 1 compaction(s)",
        )
    )


def test_repl_status_command_renders_status_without_starting_a_task(monkeypatch, capsys):
    agent = SimpleNamespace(
        model_client=SimpleNamespace(model="gpt-5.6-luna", base_url="https://api.openai.com/v1"),
        session_path="/tmp/pico-session.json",
        last_prompt_metadata={},
        _last_prefix_refresh={"workspace_changed": False},
        current_task_state=None,
    )
    inputs = iter(("/status", "/exit"))

    monkeypatch.setattr(cli, "build_agent", lambda args, *, trace_sink: agent)
    monkeypatch.setattr(cli, "build_welcome", lambda *args, **kwargs: "welcome")
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    assert cli.main([]) == 0

    assert "model: gpt-5.6-luna" in capsys.readouterr().out


def test_runs_command_lists_only_main_runs_and_undo_details(tmp_path, capsys):
    runs_root = tmp_path / ".pico" / "runs"
    main_run = "run_main"
    child_run = "run_child"
    (runs_root / main_run).mkdir(parents=True)
    (runs_root / child_run).mkdir()
    (runs_root / "index.json").write_text(
        json.dumps(
            [
                {
                    "run_id": child_run,
                    "status": "completed",
                    "updated_at": "2026-07-27T10:01:00+00:00",
                    "agent_mode": "delegate",
                    "parent_agent_id": "agent_parent",
                },
                {
                    "run_id": main_run,
                    "status": "completed",
                    "updated_at": "2026-07-27T10:00:00+00:00",
                    "agent_mode": "main",
                    "parent_agent_id": "",
                },
            ]
        ),
        encoding="utf-8",
    )
    (runs_root / main_run / "report.json").write_text(
        json.dumps(
            {
                "undo": {
                    "available": True,
                    "changed_paths": ["README.md", "src/pico.py"],
                }
            }
        ),
        encoding="utf-8",
    )

    assert cli.main(["runs", "--cwd", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert f"Pico runs: {tmp_path}" in output
    assert "run_main | completed | 2026-07-27T10:00:00+00:00" in output
    assert "undo: available | changed: README.md, src/pico.py" in output
    assert "run_child" not in output


def test_repl_runs_command_does_not_start_a_task(monkeypatch, tmp_path, capsys):
    runs_root = tmp_path / ".pico" / "runs"
    (runs_root / "run_123").mkdir(parents=True)
    (runs_root / "index.json").write_text(
        json.dumps(
            [
                {
                    "run_id": "run_123",
                    "status": "running",
                    "updated_at": "2026-07-27T10:00:00+00:00",
                    "agent_mode": "main",
                    "parent_agent_id": "",
                }
            ]
        ),
        encoding="utf-8",
    )
    agent = SimpleNamespace(
        model_client=SimpleNamespace(model="gpt-5.6-luna", base_url="https://api.openai.com/v1"),
        session_path="/tmp/pico-session.json",
        last_prompt_metadata={},
        _last_prefix_refresh={"workspace_changed": False},
        current_task_state=None,
        workspace=SimpleNamespace(repo_root=str(tmp_path)),
    )
    inputs = iter(("/runs", "/exit"))

    monkeypatch.setattr(cli, "build_agent", lambda args, *, trace_sink: agent)
    monkeypatch.setattr(cli, "build_welcome", lambda *args, **kwargs: "welcome")
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    assert cli.main([]) == 0

    output = capsys.readouterr().out
    assert "run_123 | running | 2026-07-27T10:00:00+00:00" in output
    assert "undo: pending | changed: -" in output


def test_repo_map_command_renders_rank_evidence_without_starting_an_agent(tmp_path, capsys):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "services.py").write_text(
        "def create_user():\n    return 'created'\n",
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "repo-map",
                "--cwd",
                str(tmp_path),
                "--query",
                "Fix create_user",
                "--budget-tokens",
                "300",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "app/services.py" in output
    assert "create_user" in output
    assert "repo_map_stats:" in output
    assert "rank_evidence:" in output
    assert not (tmp_path / ".pico").exists()
