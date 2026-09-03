"""Concrete host command runner for trusted, Runtime-owned verification."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .execution import ExecutionContext

DEFAULT_COMMAND_MAX_OUTPUT_BYTES = 1_048_576
COMMAND_POLL_SECONDS = 0.05
COMMAND_TERMINATE_SECONDS = 1.0


def shell_argv(command):
    """Build the POSIX shell invocation used for a configured command."""

    command = str(command or "").strip()
    if not command:
        raise ValueError("shell command must not be empty")
    return "/bin/sh", "-c", command


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    stop_reason: str = ""
    output_limited: bool = False
    infrastructure_error: bool = False


class CommandRunner:
    """Run a trusted command locally with deadline and process-group cleanup.

    Output is collected by :meth:`subprocess.Popen.communicate` and truncated
    only after the process exits.  ``max_output_bytes`` bounds the returned
    result, not peak memory while the child is running.
    """

    def __init__(self, workspace_root, *, max_output_bytes=None):
        root = Path(workspace_root).resolve()
        if not root.is_dir():
            raise ValueError(f"command workspace is not a directory: {root}")
        limit = (
            DEFAULT_COMMAND_MAX_OUTPUT_BYTES
            if max_output_bytes is None
            else int(max_output_bytes)
        )
        if limit < 1024:
            raise ValueError("command max_output_bytes must be at least 1024")
        self.workspace_root = root
        self.max_output_bytes = limit

    def run(
        self,
        argv,
        *,
        cwd,
        timeout,
        env=None,
        execution_context=None,
    ):
        argv = tuple(str(item) for item in argv)
        if not argv or any(not item for item in argv):
            raise ValueError("command argv must contain a non-empty executable")
        cwd = self._contained_cwd(cwd)
        context = execution_context or ExecutionContext.standalone(
            max_seconds=timeout
        )
        effective_timeout = context.bounded_timeout(timeout)
        deadline = min(
            time.monotonic() + effective_timeout,
            context.deadline,
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=self._environment(cwd, env or {}),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return CommandResult(
                returncode=None,
                stderr=f"{type(exc).__name__}: {exc}",
                infrastructure_error=True,
            )

        stop_reason = ""
        stdout = b""
        stderr = b""
        try:
            while True:
                if context.token.requested:
                    stop_reason = context.token.reason or "user_cancelled"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stop_reason = "deadline_exceeded"
                    break
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(COMMAND_POLL_SECONDS, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            self._terminate_process_group(process)
            raise

        if stop_reason:
            stdout, stderr = self._terminate_process_group(process)
        rendered_stdout, rendered_stderr, limited = self._truncate_output(
            stdout,
            stderr,
        )
        if not stop_reason:
            return CommandResult(
                returncode=int(process.returncode or 0),
                stdout=rendered_stdout,
                stderr=rendered_stderr,
                output_limited=limited,
            )
        return CommandResult(
            returncode=None,
            stdout=rendered_stdout,
            stderr=rendered_stderr,
            stop_reason=stop_reason,
            output_limited=limited,
        )

    def _contained_cwd(self, cwd):
        cwd = Path(cwd).resolve()
        try:
            cwd.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(f"command cwd escapes workspace: {cwd}") from exc
        if not cwd.is_dir():
            raise ValueError(f"command cwd is not a directory: {cwd}")
        return cwd

    def _environment(self, cwd, provided):
        environment = {
            "HOME": tempfile.gettempdir(),
            "PATH": os.environ.get("PATH", os.defpath),
            "PWD": str(cwd),
            "TMPDIR": tempfile.gettempdir(),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "RUFF_CACHE_DIR": str(Path(tempfile.gettempdir()) / "pico-ruff-cache"),
            "PYTHONPATH": str(
                self.workspace_root / "src"
                if (self.workspace_root / "src").is_dir()
                else self.workspace_root
            ),
        }
        for name, value in dict(provided).items():
            name = str(name)
            value = str(value)
            if not name or "=" in name or "\x00" in name or "\x00" in value:
                raise ValueError("command environment contains an invalid entry")
            environment[name] = value
        for name in ("HOME", "PATH", "PWD", "TMPDIR"):
            environment[name] = {
                "HOME": tempfile.gettempdir(),
                "PATH": os.environ.get("PATH", os.defpath),
                "PWD": str(cwd),
                "TMPDIR": tempfile.gettempdir(),
            }[name]
        return environment

    @staticmethod
    def _terminate_process_group(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=COMMAND_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass
            stdout, stderr = process.communicate()
        return stdout, stderr

    def _truncate_output(self, stdout, stderr):
        stdout = bytes(stdout or b"")
        stderr = bytes(stderr or b"")
        limit = self.max_output_bytes
        if len(stdout) + len(stderr) <= limit:
            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
                False,
            )
        stdout_budget = limit // 2
        stderr_budget = limit - stdout_budget
        if len(stdout) < stdout_budget:
            stderr_budget += stdout_budget - len(stdout)
            stdout_budget = len(stdout)
        elif len(stderr) < stderr_budget:
            stdout_budget += stderr_budget - len(stderr)
            stderr_budget = len(stderr)
        return (
            stdout[:stdout_budget].decode("utf-8", errors="replace"),
            stderr[:stderr_budget].decode("utf-8", errors="replace"),
            True,
        )
