import sys

from pico.command_runner import CommandRunner


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
