from types import SimpleNamespace

from pico.cli import (
    _outcome_summary,
    _unfinished_session,
    detect_verification_command,
    resolve_verification_command,
)
from pico.session_store import Session, SessionStore


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

    assert store.latest_active() == "active"


def test_cli_reports_the_unfinished_run_without_resuming_it(tmp_path):
    store = SessionStore(tmp_path / ".pico" / "sessions")
    store.save(_session(store, "session_active", tmp_path, "run_active"))

    assert _unfinished_session(tmp_path) == {
        "session_id": "session_active",
        "run_id": "run_active",
    }


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
