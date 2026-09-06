import os
import signal
import sys
import threading
import time

import pytest

from pico.command_runner import CommandRunner
from pico.execution import ExecutionContext


def test_command_timeout_returns_one_stop_reason(tmp_path):
    result = CommandRunner(tmp_path).run(
        (sys.executable, "-c", "import time; time.sleep(10)"),
        cwd=tmp_path,
        timeout=0.05,
        env={},
    )

    assert result.returncode is None
    assert result.stop_reason == "deadline_exceeded"


def test_command_start_failure_is_an_infrastructure_error(tmp_path):
    result = CommandRunner(tmp_path).run(
        ("pico-command-that-does-not-exist",),
        cwd=tmp_path,
        timeout=1,
        env={},
    )

    assert result.returncode is None
    assert result.infrastructure_error is True
    assert "FileNotFoundError" in result.stderr


def test_command_output_limit_preserves_the_one_consumed_flag(tmp_path):
    result = CommandRunner(tmp_path, max_output_bytes=1024).run(
        (sys.executable, "-c", "print('x' * 2048)"),
        cwd=tmp_path,
        timeout=2,
        env={},
    )

    assert result.returncode == 0
    assert result.output_limited is True
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024


@pytest.mark.parametrize("stop", ["deadline_exceeded", "user_cancelled"])
def test_cleanup_is_bounded_when_detached_descendant_keeps_output_pipes(tmp_path, stop):
    pid_file = tmp_path / "detached.pid"
    code = (
        "import subprocess, sys; from pathlib import Path; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], "
        "start_new_session=True); Path('detached.pid').write_text(str(p.pid)); "
        "print('ready', flush=True); print('diagnostic', file=sys.stderr, flush=True)"
    )
    context = ExecutionContext.root(max_seconds=30)
    timer = threading.Timer(0.5, lambda: context.request_stop("user_cancelled"))
    started = time.monotonic()
    if stop == "user_cancelled":
        timer.start()
    try:
        result = CommandRunner(tmp_path).run(
            (sys.executable, "-B", "-c", code), cwd=tmp_path,
            timeout=0.5 if stop == "deadline_exceeded" else 30,
            execution_context=context,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 4.5
        assert result.stop_reason == stop
        assert result.returncode is None
        assert "ready" in result.stdout
        assert "diagnostic" in result.stderr
    finally:
        timer.cancel()
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cleanup_kills_a_child_that_ignores_sigterm(tmp_path):
    code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(10)"
    )
    started = time.monotonic()
    result = CommandRunner(tmp_path).run(
        (sys.executable, "-B", "-c", code), cwd=tmp_path, timeout=0.5,
    )
    assert time.monotonic() - started < 4.5
    assert result.stop_reason == "deadline_exceeded"
    assert "ready" in result.stdout
