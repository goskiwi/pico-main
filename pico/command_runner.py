"""Bounded host command execution for model-requested shell tools."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .execution import ExecutionContext

DEFAULT_COMMAND_MAX_OUTPUT_BYTES = 1_048_576
COMMAND_POLL_SECONDS = 0.05
COMMAND_TERMINATE_SECONDS = 1.0
COMMAND_PIPE_GRACE_SECONDS = 1.0
COMMAND_READ_CHUNK_BYTES = 64 * 1024


class _HeadTailBuffer:
    """Keep a fixed prefix and suffix while counting omitted middle bytes."""

    def __init__(self, max_bytes):
        self.head_limit = int(max_bytes) // 2
        self.tail_limit = int(max_bytes) - self.head_limit
        self.head = bytearray()
        self.tail = deque()
        self.tail_bytes = 0
        self.omitted_bytes = 0

    def append(self, chunk):
        chunk = bytes(chunk)
        head_remaining = self.head_limit - len(self.head)
        if head_remaining > 0:
            selected = chunk[:head_remaining]
            self.head.extend(selected)
            chunk = chunk[len(selected):]
        if not chunk:
            return
        self.tail.append(chunk)
        self.tail_bytes += len(chunk)
        excess = max(0, self.tail_bytes - self.tail_limit)
        self.omitted_bytes += excess
        while excess:
            first = self.tail[0]
            if len(first) <= excess:
                self.tail.popleft()
                self.tail_bytes -= len(first)
                excess -= len(first)
            else:
                self.tail[0] = first[excess:]
                self.tail_bytes -= excess
                excess = 0

    @property
    def head_bytes(self):
        return len(self.head)

    def retained(self):
        return bytes(self.head) + b"".join(self.tail)


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
    """Bounded process output retained as bytes for Runtime formatting."""

    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stop_reason: str = ""
    output_limited: bool = False
    stdout_discarded_bytes: int = 0
    stderr_discarded_bytes: int = 0
    stdout_head_bytes: int = 0
    stderr_head_bytes: int = 0
    infrastructure_error: bool = False


class CommandRunner:
    """Run a trusted command locally with deadline and process-group cleanup.

    A selector waits for pipe readiness and drains into bounded buffers. Bytes
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
        output_log=None,
    ):
        raw = self.run_bytes(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            execution_context=execution_context,
            input_bytes=input_bytes,
            output_log=output_log,
        )
        rendered_stdout = self._render_output(
            raw.stdout,
            "stdout",
            raw.stdout_discarded_bytes,
            raw.stdout_head_bytes,
        )
        rendered_stderr = self._render_output(
            raw.stderr,
            "stderr",
            raw.stderr_discarded_bytes,
            raw.stderr_head_bytes,
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
        output_log=None,
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
                stdin=(
                    subprocess.PIPE
                    if input_bytes is not None
                    else subprocess.DEVNULL
                ),
                start_new_session=True,
                bufsize=0,
            )
        except OSError as exc:
            return RawCommandResult(
                returncode=None,
                stderr=f"{type(exc).__name__}: {exc}".encode(
                    "utf-8", errors="replace"
                ),
                infrastructure_error=True,
            )

        stream_limit = self.max_output_bytes // 2
        buffers = {
            "stdout": _HeadTailBuffer(stream_limit),
            "stderr": _HeadTailBuffer(stream_limit),
        }
        try:
            stop_reason = self._collect_output(
                process, context, deadline, input_bytes, buffers, output_log
            )
        except BaseException:
            self._stop_process_group(process)
            raise
        finally:
            self._close_pipes(process)
        limited = bool(
            buffers["stdout"].omitted_bytes
            or buffers["stderr"].omitted_bytes
        )
        infrastructure_error = bool(require_complete_output and limited)
        return RawCommandResult(
            returncode=(
                None
                if stop_reason and stop_reason != "pipe_held_open"
                else int(process.returncode or 0)
            ),
            stdout=buffers["stdout"].retained(),
            stderr=buffers["stderr"].retained(),
            stop_reason=stop_reason,
            output_limited=limited,
            stdout_discarded_bytes=buffers["stdout"].omitted_bytes,
            stderr_discarded_bytes=buffers["stderr"].omitted_bytes,
            stdout_head_bytes=buffers["stdout"].head_bytes,
            stderr_head_bytes=buffers["stderr"].head_bytes,
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
        environment = {}
        for name, value in dict(provided).items():
            name = str(name)
            value = str(value)
            if not name or "=" in name or "\x00" in name or "\x00" in value:
                raise ValueError("command environment contains an invalid entry")
            environment[name] = value
        environment.update(
            {
                "HOME": tempfile.gettempdir(),
                "PATH": os.environ.get("PATH", os.defpath),
                "PWD": str(cwd),
                "TMPDIR": tempfile.gettempdir(),
            }
        )
        return environment

    @staticmethod
    def _signal_process_group(process, sig):
        try:
            os.killpg(process.pid, sig)
        except OSError:
            pass

    def _collect_output(self, process, context, deadline, input_bytes, buffers, output_log):
        stop_reason = ""
        pipe_deadline = None
        input_view = memoryview(input_bytes or b"")
        with selectors.DefaultSelector() as selector:
            for name in ("stdout", "stderr"):
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin is not None:
                if input_view:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while True:
                now = time.monotonic()
                exited = process.poll() is not None
                if exited and not selector.get_map():
                    break
                if not stop_reason and (context.token.requested or now >= deadline):
                    stop_reason = (
                        context.token.reason or "user_cancelled"
                        if context.token.requested else "deadline_exceeded"
                    )
                    self._stop_process_group(process)
                    pipe_deadline = time.monotonic() + COMMAND_PIPE_GRACE_SECONDS
                if exited and pipe_deadline is None:
                    pipe_deadline = now + COMMAND_PIPE_GRACE_SECONDS
                if pipe_deadline is not None and time.monotonic() >= pipe_deadline:
                    if not stop_reason:
                        stop_reason = "pipe_held_open"
                        self._stop_process_group(process)
                    break
                next_deadline = pipe_deadline if stop_reason else min(deadline, pipe_deadline or deadline)
                wait = max(0.0, min(COMMAND_POLL_SECONDS, next_deadline - time.monotonic()))
                for key, _mask in selector.select(wait):
                    input_view = self._consume_ready_pipe(
                        selector, key, input_view, buffers, output_log
                    )
        return stop_reason

    def _consume_ready_pipe(self, selector, key, input_view, buffers, output_log):
        stream, name = key.fileobj, key.data
        try:
            if name == "stdin":
                try:
                    written = os.write(stream.fileno(), input_view[:COMMAND_READ_CHUNK_BYTES])
                    input_view = input_view[written:]
                except BrokenPipeError:
                    input_view = input_view[len(input_view):]
                if not input_view:
                    selector.unregister(stream)
                    stream.close()
            else:
                chunk = os.read(stream.fileno(), COMMAND_READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(stream)
                else:
                    if output_log is not None:
                        output_log.write(name, chunk)
                    buffers[name].append(chunk)
        except BlockingIOError:
            pass  # Readiness may change before the nonblocking operation.
        return input_view

    @classmethod
    def _stop_process_group(cls, process):
        cls._signal_process_group(process, signal.SIGTERM)
        leader_stopped = False
        try:
            process.wait(timeout=COMMAND_TERMINATE_SECONDS)
            leader_stopped = True
        except subprocess.TimeoutExpired:
            pass

        # The group can still contain descendants after its leader exits on
        # SIGTERM. Always close the escalation boundary for the whole group.
        cls._signal_process_group(process, signal.SIGKILL)
        if leader_stopped:
            return
        try:
            process.wait(timeout=COMMAND_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("process group did not stop after SIGKILL") from exc

    @staticmethod
    def _close_pipes(process):
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    @staticmethod
    def _render_output(payload, name, discarded_bytes, head_bytes):
        payload = bytes(payload or b"")
        if discarded_bytes:
            marker = (
                f"\n[{name} middle omitted; discarded "
                f"{discarded_bytes} bytes]\n"
            ).encode()
            payload = payload[:head_bytes] + marker + payload[head_bytes:]
        return payload.decode("utf-8", errors="replace")
