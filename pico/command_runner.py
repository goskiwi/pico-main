"""Concrete host command runner for trusted, Runtime-owned verification."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .execution import ExecutionContext

DEFAULT_COMMAND_MAX_OUTPUT_BYTES = 1_048_576
COMMAND_POLL_SECONDS = 0.05
COMMAND_TERMINATE_SECONDS = 1.0
COMMAND_PIPE_GRACE_SECONDS = 1.0
COMMAND_READ_CHUNK_BYTES = 64 * 1024


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
    stdout_discarded_bytes: int = 0
    stderr_discarded_bytes: int = 0
    infrastructure_error: bool = False


@dataclass(frozen=True)
class RawCommandResult:
    """Unmodified process output for Runtime observers that need exact bytes."""

    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stop_reason: str = ""
    output_limited: bool = False
    stdout_discarded_bytes: int = 0
    stderr_discarded_bytes: int = 0
    infrastructure_error: bool = False


class CommandRunner:
    """Run a trusted command locally with deadline and process-group cleanup.

    Reader threads continuously drain both pipes into bounded buffers. Bytes
    beyond the limit are counted and discarded while the process continues.
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
        input_bytes=None,
    ):
        raw = self.run_bytes(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            execution_context=execution_context,
            input_bytes=input_bytes,
        )
        rendered_stdout = self._render_output(
            raw.stdout, "stdout", raw.stdout_discarded_bytes
        )
        rendered_stderr = self._render_output(
            raw.stderr, "stderr", raw.stderr_discarded_bytes
        )
        return CommandResult(
            returncode=raw.returncode,
            stdout=rendered_stdout,
            stderr=rendered_stderr,
            stop_reason=raw.stop_reason,
            output_limited=raw.output_limited,
            stdout_discarded_bytes=raw.stdout_discarded_bytes,
            stderr_discarded_bytes=raw.stderr_discarded_bytes,
            infrastructure_error=raw.infrastructure_error,
        )

    def run_bytes(
        self,
        argv,
        *,
        cwd,
        timeout,
        env=None,
        execution_context=None,
        input_bytes=None,
        require_complete_output=False,
    ):
        """Run one process while preserving stdout and stderr as exact bytes."""

        argv = tuple(str(item) for item in argv)
        if not argv or any(not item for item in argv):
            raise ValueError("command argv must contain a non-empty executable")
        cwd = self._contained_cwd(cwd)
        if input_bytes is not None and not isinstance(
            input_bytes, (bytes, bytearray)
        ):
            raise TypeError("command input must be bytes or null")
        input_bytes = None if input_bytes is None else bytes(input_bytes)
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
                stdin=(subprocess.PIPE if input_bytes is not None else None),
                start_new_session=True,
            )
        except OSError as exc:
            return RawCommandResult(
                returncode=None,
                stderr=f"{type(exc).__name__}: {exc}".encode(
                    "utf-8", errors="replace"
                ),
                infrastructure_error=True,
            )

        stop_reason = ""
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        discarded = {"stdout": 0, "stderr": 0}
        stream_limit = max(1, self.max_output_bytes // 2)

        def drain(name, stream):
            try:
                while True:
                    chunk = stream.read(COMMAND_READ_CHUNK_BYTES)
                    if not chunk:
                        return
                    available = max(0, stream_limit - len(buffers[name]))
                    buffers[name].extend(chunk[:available])
                    discarded[name] += max(0, len(chunk) - available)
            except (OSError, ValueError):
                return

        readers = [
            threading.Thread(
                target=drain,
                args=(name, stream),
                name=f"pico-{name}-drain",
                daemon=True,
            )
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        ]
        for reader in readers:
            reader.start()
        try:
            if input_bytes is not None and process.stdin is not None:
                process.stdin.write(input_bytes)
                process.stdin.close()
            stop_reason = self._wait_for_process(process, context, deadline)
        except BaseException:
            self._signal_process_group(process, signal.SIGKILL)
            self._close_pipes(process)
            raise

        if stop_reason:
            self._stop_process_group(process)
        else:
            pipe_deadline = time.monotonic() + COMMAND_PIPE_GRACE_SECONDS
            for reader in readers:
                reader.join(max(0.0, pipe_deadline - time.monotonic()))
            if any(reader.is_alive() for reader in readers):
                stop_reason = "pipe_held_open"
                self._stop_process_group(process)

        self._close_pipes(process)
        for reader in readers:
            reader.join(COMMAND_POLL_SECONDS)
        limited = bool(discarded["stdout"] or discarded["stderr"])
        infrastructure_error = bool(require_complete_output and limited)
        return RawCommandResult(
            returncode=(
                None
                if stop_reason and stop_reason != "pipe_held_open"
                else int(process.returncode or 0)
            ),
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            stop_reason=stop_reason,
            output_limited=limited,
            stdout_discarded_bytes=discarded["stdout"],
            stderr_discarded_bytes=discarded["stderr"],
            infrastructure_error=infrastructure_error,
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
    def _signal_process_group(process, sig):
        try:
            os.killpg(process.pid, sig)
        except OSError:
            pass

    @staticmethod
    def _wait_for_process(process, context, deadline):
        while process.poll() is None:
            if context.token.requested:
                return context.token.reason or "user_cancelled"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "deadline_exceeded"
            context.token.wait(min(COMMAND_POLL_SECONDS, remaining))
        return ""

    @classmethod
    def _stop_process_group(cls, process):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            cls._signal_process_group(process, sig)
            try:
                process.wait(timeout=COMMAND_TERMINATE_SECONDS)
            except subprocess.TimeoutExpired as exc:
                if sig == signal.SIGKILL:
                    raise RuntimeError("process group did not stop after SIGKILL") from exc

    @staticmethod
    def _close_pipes(process):
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    @staticmethod
    def _render_output(payload, name, discarded_bytes):
        text = bytes(payload or b"").decode("utf-8", errors="replace")
        if discarded_bytes:
            marker = f"[{name} truncated; discarded {discarded_bytes} bytes]"
            text = text.rstrip("\n") + "\n" + marker
        return text
