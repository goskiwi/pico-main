import json
import os
import sys
import threading
import time

import pytest

from pico.command_runner import CommandRunner, shell_argv
from pico.execution import ExecutionContext


def test_command_runner_contains_cwd_and_starts_a_new_session(tmp_path):
    runner = CommandRunner(tmp_path)

    with pytest.raises(ValueError, match="cwd escapes workspace"):
        runner.run((sys.executable, "-c", "pass"), cwd=tmp_path.parent, timeout=2)

    result = runner.run(
        (
            sys.executable,
            "-c",
            "import json, os; print(json.dumps({'sid': os.getsid(0)}))",
        ),
        cwd=tmp_path,
        timeout=2,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["sid"] != os.getsid(0)


def test_command_runner_uses_a_minimal_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PICO_OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("UNRELATED_PARENT_SECRET", "must-not-leak")
    runner = CommandRunner(tmp_path)

    result = runner.run(
        (
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(dict(os.environ), sort_keys=True))",
        ),
        cwd=tmp_path,
        timeout=2,
        env={"LANG": "C", "EXPLICIT_VALUE": "visible"},
    )
    environment = json.loads(result.stdout)

    assert result.returncode == 0
    assert environment["EXPLICIT_VALUE"] == "visible"
    assert environment["LANG"] == "C"
    assert "PICO_OPENAI_API_KEY" not in environment
    assert "UNRELATED_PARENT_SECRET" not in environment
    assert {"HOME", "PATH", "PWD", "TMPDIR", "PYTHONPATH"} <= set(environment)


def test_command_runner_timeout_kills_the_process_group(tmp_path):
    marker = tmp_path / "child-survived.txt"
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(0.5); Path({str(marker)!r}).write_text('survived')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(10)"
    )

    result = CommandRunner(tmp_path).run(
        (sys.executable, "-c", parent),
        cwd=tmp_path,
        timeout=0.1,
    )
    time.sleep(0.6)

    assert result.returncode is None
    assert result.timed_out is True
    assert result.cancelled is False
    assert result.killed is True
    assert result.cleanup_state == "completed"
    assert result.stop_reason == "deadline_exceeded"
    assert not marker.exists()


def test_command_runner_cancellation_kills_the_process_group(tmp_path):
    context = ExecutionContext.root(max_seconds=5)
    timer = threading.Timer(0.1, lambda: context.request_stop("user_cancelled"))
    timer.start()
    try:
        result = CommandRunner(tmp_path).run(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            cwd=tmp_path,
            timeout=5,
            execution_context=context,
        )
    finally:
        timer.cancel()

    assert result.returncode is None
    assert result.cancelled is True
    assert result.timed_out is False
    assert result.killed is True
    assert result.cleanup_state == "completed"
    assert result.stop_reason == "user_cancelled"


def test_command_runner_truncates_only_the_returned_output(tmp_path):
    result = CommandRunner(tmp_path, max_output_bytes=1024).run(
        (
            sys.executable,
            "-c",
            "import sys; print('o' * 2000); print('e' * 2000, file=sys.stderr)",
        ),
        cwd=tmp_path,
        timeout=2,
    )

    assert result.returncode == 0
    assert result.output_limited is True
    assert result.stop_reason == ""
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024


def test_shell_argv_rejects_empty_commands():
    assert shell_argv("echo ok") == ("/bin/sh", "-c", "echo ok")
    with pytest.raises(ValueError, match="must not be empty"):
        shell_argv("  ")
