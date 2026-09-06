from types import SimpleNamespace

from pico import FakeModelClient, ModelAction, Pico, PicoConfig, Workspace, cli
from pico.cli import (
    _outcome_summary,
    detect_verification_command,
    resolve_verification_command,
)
from pico.run_cli import run_main
from pico.run_lifecycle import RunLifecycle
from pico.run_log import RunLog
from pico.run_store import RunStore
from pico.session_store import Session, SessionStore
from pico.task_state import TaskContract


def _session(store, session_id, root, active_run_id):
    return Session(store, session_id, root, active_run_id)


def test_cli_detects_pytest_without_user_configuration(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text("def test_ok():\n    assert True\n")

    command = detect_verification_command(tmp_path)

    assert command.endswith(" -m pytest -q")


def test_cli_does_not_guess_a_verifier_without_python_tests(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

    assert detect_verification_command(tmp_path) == ""


def test_explicit_verifier_overrides_auto_detection(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text("def test_ok():\n    assert True\n")

    assert resolve_verification_command(tmp_path, "make check") == "make check"


def test_latest_active_ignores_newer_completed_sessions(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    store.save(_session(store, "active", tmp_path, "run_active"))
    store.save(_session(store, "completed", tmp_path, ""))

    runs = RunStore(tmp_path / ".pico" / "runs")
    RunLog("run_active", "task_active", "active", runs).append_user(TaskContract("active", True, False))
    assert store.latest_active(runs) == "active"


def test_resume_latest_skips_stale_terminal_pointer(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / ".pico/sessions")
    older = Pico(FakeModelClient([]), Workspace.build(tmp_path), config=PicoConfig(mode="auto"),
                 session=store.create(tmp_path))
    RunLifecycle(older).initialize("Unfinished original task")
    newer = Pico(FakeModelClient([ModelAction.final("done")]), Workspace.build(tmp_path),
                 config=older.config, session=store.create(tmp_path))
    finished = newer.ask("Finished task")
    newer.session.set_active_run(finished.run_id)
    args = cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--resume", "latest"])
    monkeypatch.setattr(cli, "_build_model_client", lambda _args: FakeModelClient([]))
    selected = cli.build_agent(args)
    assert selected.session.id == older.session.id
    assert selected.run.projection.contract.goal == "Unfinished original task"
    assert store.load(newer.session.id).active_run_id == ""


def test_run_commands_resolve_the_repository_root_from_subdirectories(tmp_path, capsys):
    import subprocess

    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subdirectory = tmp_path / "src"
    subdirectory.mkdir()
    agent = Pico(FakeModelClient([ModelAction.final("done")]), Workspace.build(subdirectory),
                 config=PicoConfig(mode="auto"), session=SessionStore(tmp_path / ".pico/sessions").create(tmp_path))
    outcome = agent.ask("Answer")
    for command in ("show", "events"):
        assert run_main([command, outcome.run_id, "--cwd", str(subdirectory)]) == 0
        assert outcome.run_id in capsys.readouterr().out


def test_outcome_summary_uses_runtime_facts():
    agent = SimpleNamespace(
        config=SimpleNamespace(verification_command="python -m pytest -q"),
        run=SimpleNamespace(
            evidence=SimpleNamespace(verifications=[{"status": "passed"}])
        ),
    )
    outcome = SimpleNamespace(
        status="completed",
        stop_reason="final_answer_returned",
        changed_paths=("src/app.py",),
        run_id="run_example",
    )

    assert _outcome_summary(agent, outcome) == (
        "Status: completed\n"
        "Changed: src/app.py\n"
        "Verification: passed\n"
        "Run: run_example"
    )


def test_no_change_outcome_does_not_claim_verification_passed():
    agent = SimpleNamespace(
        config=SimpleNamespace(verification_command="python -m pytest -q"),
        run=SimpleNamespace(evidence=SimpleNamespace(verifications=[])),
    )
    outcome = SimpleNamespace(
        status="completed",
        stop_reason="final_answer_returned",
        changed_paths=(),
        run_id="run_example",
    )

    assert "Verification: not required" in _outcome_summary(agent, outcome)
