"""Mandatory Docker isolation for model-requested shell commands."""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .execution import ExecutionBudget, ExecutionContext

DEFAULT_SANDBOX_IMAGE = "pico/sandbox:latest"
DEFAULT_SANDBOX_CPUS = 4.0
DEFAULT_SANDBOX_MEMORY = "4g"
DEFAULT_SANDBOX_PIDS_LIMIT = 512
DEFAULT_SANDBOX_MAX_OUTPUT_BYTES = 1_048_576
SANDBOX_OUTPUT_CHUNK_BYTES = 65_536
SANDBOX_OUTPUT_QUEUE_CHUNKS = 8
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_PATH = "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
CONTAINER_TIKTOKEN_CACHE_DIR = "/opt/pico/tiktoken-cache"
HOST_ENV_DENYLIST = {
    "HOME",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
}

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


class SandboxProfile(str, Enum):
    """Runtime-owned execution profiles; neither permits persistent shell writes."""

    INSPECT = "inspect"
    VERIFY = "verify"

    @property
    def workspace_read_only(self):
        return True


def parse_command_invocation(command):
    """Split a trusted command once into direct argv and explicit environment."""
    parts = shlex.split(str(command or ""))
    command_env = {}
    while parts and _ENV_ASSIGNMENT_RE.fullmatch(parts[0]):
        name, value = parts.pop(0).split("=", 1)
        command_env[name] = value
    if not parts:
        raise ValueError("command must contain an executable")
    return tuple(parts), command_env


class SandboxError(RuntimeError):
    """Base error with stable audit metadata."""

    code = "sandbox_failed"
    security_event_type = "sandbox_failure"


class SandboxUnavailableError(SandboxError):
    code = "sandbox_unavailable"
    security_event_type = "sandbox_unavailable"


class SandboxImageMissingError(SandboxError):
    code = "sandbox_image_missing"
    security_event_type = "sandbox_image_missing"


@dataclass(frozen=True)
class SandboxResult:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    killed: bool = False
    cleanup_state: str = "not_required"
    stop_reason: str = ""
    output_limited: bool = False
    output_truncated: bool = False
    observed_output_bytes: int = 0
    max_output_bytes: int = 0
    retained_output_bytes: int = 0


@dataclass(frozen=True)
class DockerSandboxConfig:
    image: str = DEFAULT_SANDBOX_IMAGE
    cpus: float = DEFAULT_SANDBOX_CPUS
    memory: str = DEFAULT_SANDBOX_MEMORY
    pids_limit: int = DEFAULT_SANDBOX_PIDS_LIMIT
    max_output_bytes: int = DEFAULT_SANDBOX_MAX_OUTPUT_BYTES

    def __post_init__(self):
        if not str(self.image).strip():
            raise ValueError("sandbox image must not be empty")
        if float(self.cpus) <= 0:
            raise ValueError("sandbox cpus must be greater than zero")
        if not str(self.memory).strip():
            raise ValueError("sandbox memory must not be empty")
        if int(self.pids_limit) < 16:
            raise ValueError("sandbox pids_limit must be at least 16")
        if int(self.max_output_bytes) < 1024:
            raise ValueError("sandbox max_output_bytes must be at least 1024")


@dataclass
class _CaptureState:
    chunks: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=SANDBOX_OUTPUT_QUEUE_CHUNKS)
    )
    stop_readers: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)
    retained: dict[str, bytearray] = field(
        default_factory=lambda: {"stdout": bytearray(), "stderr": bytearray()}
    )
    observed: int = 0
    stop_reason: str = ""
    cleanup_state: str = "not_required"
    killed: bool = False
    cancelled: bool = False
    timed_out: bool = False
    output_limited: bool = False


class DockerSandbox:
    """Run commands in an ephemeral, resource-limited Docker container."""

    backend = "docker"

    def __init__(self, workspace_root, config=None, docker_binary=None):
        self.workspace_root = Path(workspace_root).resolve()
        self.config = config or DockerSandboxConfig()
        self.docker_binary = str(docker_binary or shutil.which("docker") or "docker")

    def ensure_ready(self):
        if not shutil.which(self.docker_binary) and not Path(self.docker_binary).exists():
            raise SandboxUnavailableError(
                "Docker is required for run_shell but the docker CLI was not found."
            )
        daemon = self._readiness_command(
            [self.docker_binary, "version", "--format", "{{.Server.Version}}"]
        )
        if daemon.returncode != 0:
            detail = (daemon.stderr or daemon.stdout).strip()
            raise SandboxUnavailableError(
                "Docker is required for run_shell but the daemon is unavailable"
                + (f": {detail}" if detail else ".")
            )
        image = self._readiness_command(
            [self.docker_binary, "image", "inspect", self.config.image]
        )
        if image.returncode != 0:
            raise SandboxImageMissingError(
                f"Docker sandbox image '{self.config.image}' is not available. "
                "Build it with: docker build -f docker/sandbox.Dockerfile -t "
                f"{self.config.image} ."
            )

    @staticmethod
    def _readiness_command(args):
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(f"Docker readiness check failed: {exc}") from exc

    def run(
        self,
        argv,
        *,
        cwd,
        timeout,
        env=None,
        execution_context=None,
        profile=SandboxProfile.INSPECT,
    ):
        self.ensure_ready()
        profile = SandboxProfile(profile)
        argv = tuple(str(item) for item in argv)
        if not argv or any(not item for item in argv):
            raise ValueError("sandbox argv must contain a non-empty executable")
        context = execution_context or ExecutionContext.standalone(
            owner="docker_sandbox", max_seconds=timeout
        )
        effective_timeout = context.bounded_timeout(timeout)
        cwd = Path(cwd).resolve()
        try:
            relative_cwd = cwd.relative_to(self.workspace_root)
        except ValueError as exc:
            raise SandboxError(f"sandbox cwd escapes workspace: {cwd}") from exc

        container_name = self.container_name_for_execution(context.execution_id)
        container_cwd = Path(CONTAINER_WORKSPACE, relative_cwd).as_posix()
        docker_args = self._docker_args(
            container_name=container_name,
            container_cwd=container_cwd,
            argv=argv,
            env=env or {},
            profile=profile,
        )
        command_deadline = time.monotonic() + effective_timeout
        budget = ExecutionBudget(
            deadline=min(command_deadline, context.deadline),
            max_output_bytes=self.config.max_output_bytes,
            max_processes=self.config.pids_limit,
            memory=self.config.memory,
        )
        try:
            process = subprocess.Popen(
                docker_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxUnavailableError(f"Could not start Docker sandbox: {exc}") from exc
        context.transition("running")
        return self._capture_process(
            process,
            container_name=container_name,
            context=context,
            budget=budget,
        )

    @staticmethod
    def container_name_for_execution(execution_id):
        normalized = "".join(
            character for character in str(execution_id or "").lower()
            if character.isalnum()
        )
        if not normalized:
            normalized = uuid.uuid4().hex
        return "pico-" + normalized[-24:]

    def cleanup_execution(self, execution_id):
        """Remove a deterministic orphan container before recovery continues."""
        container_name = self.container_name_for_execution(execution_id)
        cleanup_state, killed = self._stop_container(container_name)
        return {
            "container_name": container_name,
            "cleanup_state": cleanup_state,
            "killed": bool(killed),
        }

    def _capture_process(self, process, *, container_name, context, budget):
        # Pipe readers must never be able to outrun the Runtime and accumulate
        # an unbounded host-memory backlog. The retained result has its own
        # byte budget; this small bounded transport queue applies backpressure
        # before the main loop has classified and stopped an output bomb.
        state = self._start_capture(process)
        try:
            self._monitor_capture(
                process,
                container_name=container_name,
                context=context,
                budget=budget,
                state=state,
            )
        except BaseException:
            self._interrupt_capture(
                process,
                container_name=container_name,
                context=context,
                state=state,
            )
            raise
        finally:
            self._finish_readers(state)
        return self._capture_result(process, context, budget, state)

    def _start_capture(self, process):
        state = _CaptureState()
        streams = {"stdout": process.stdout, "stderr": process.stderr}
        state.threads = [
            threading.Thread(
                target=self._drain_stream,
                args=(state, name, stream),
                daemon=True,
            )
            for name, stream in streams.items()
        ]
        for thread in state.threads:
            thread.start()
        return state

    @staticmethod
    def _drain_stream(state, name, stream):
        try:
            while not state.stop_readers.is_set():
                chunk = stream.read(SANDBOX_OUTPUT_CHUNK_BYTES)
                if not chunk:
                    break
                item = (name, bytes(chunk))
                while not state.stop_readers.is_set():
                    try:
                        state.chunks.put(item, timeout=0.05)
                        break
                    except queue.Full:
                        continue
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _monitor_capture(self, process, *, container_name, context, budget, state):
        while self._capture_active(process, state):
            self._consume_capture_chunk(state, budget)
            self._observe_capture_stop(context, budget, state)
            if state.stop_reason:
                state.stop_readers.set()
                self._stop_capture_process(
                    process,
                    container_name=container_name,
                    context=context,
                    state=state,
                    reason=state.stop_reason,
                )

    @staticmethod
    def _capture_active(process, state):
        return (
            process.poll() is None
            or any(thread.is_alive() for thread in state.threads)
            or not state.chunks.empty()
        )

    @staticmethod
    def _consume_capture_chunk(state, budget):
        try:
            name, chunk = state.chunks.get(timeout=0.05)
        except queue.Empty:
            return
        state.observed += len(chunk)
        retained_bytes = sum(len(value) for value in state.retained.values())
        remaining = max(0, budget.max_output_bytes - retained_bytes)
        if remaining:
            state.retained[name].extend(chunk[:remaining])
        if state.observed > budget.max_output_bytes and not state.stop_reason:
            state.output_limited = True
            state.stop_reason = "output_limit_exceeded"

    @staticmethod
    def _observe_capture_stop(context, budget, state):
        if not state.stop_reason and context.token.requested:
            state.cancelled = True
            state.stop_reason = context.token.reason or "user_cancelled"
        if not state.stop_reason and time.monotonic() >= budget.deadline:
            state.timed_out = True
            state.stop_reason = "deadline_exceeded"

    def _stop_capture_process(
        self,
        process,
        *,
        container_name,
        context,
        state,
        reason,
    ):
        if state.cleanup_state != "not_required":
            return
        context.transition(
            "stop_requested", cleanup_state="pending", stop_reason=reason
        )
        state.cleanup_state, state.killed = self._stop_container(container_name)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            state.killed = True

    def _interrupt_capture(self, process, *, container_name, context, state):
        interrupted_reason = context.token.reason or "host_interrupted"
        state.stop_readers.set()
        self._stop_capture_process(
            process,
            container_name=container_name,
            context=context,
            state=state,
            reason=interrupted_reason,
        )
        terminal_state = (
            "killed"
            if state.killed or not context.token.requested
            else "cancelled"
        )
        context.transition(
            terminal_state,
            cleanup_state=state.cleanup_state,
            stop_reason=interrupted_reason,
        )

    @staticmethod
    def _finish_readers(state):
        state.stop_readers.set()
        for thread in state.threads:
            thread.join(timeout=1)

    @staticmethod
    def _capture_result(process, context, budget, state):
        stdout = state.retained["stdout"].decode("utf-8", errors="replace")
        stderr = state.retained["stderr"].decode("utf-8", errors="replace")
        if state.stop_reason:
            terminal_state = "killed" if state.killed or state.output_limited else (
                "cancelled" if state.cancelled else "timed_out"
            )
            context.transition(
                terminal_state,
                cleanup_state=state.cleanup_state,
                stop_reason=state.stop_reason,
            )
            message = (
                f"sandbox command {terminal_state}: {state.stop_reason}; "
                f"cleanup={state.cleanup_state}"
            )
            stderr = "\n".join(part for part in (stderr.strip(), message) if part)

        return SandboxResult(
            returncode=None if state.stop_reason else int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            timed_out=state.timed_out,
            cancelled=state.cancelled,
            killed=bool(state.killed or state.output_limited),
            cleanup_state=state.cleanup_state,
            stop_reason=state.stop_reason,
            output_limited=state.output_limited,
            output_truncated=state.observed > budget.max_output_bytes,
            observed_output_bytes=state.observed,
            max_output_bytes=budget.max_output_bytes,
            retained_output_bytes=sum(
                len(value) for value in state.retained.values()
            ),
        )

    def _stop_container(self, container_name):
        try:
            stopped = subprocess.run(
                [self.docker_binary, "stop", "--time", "2", container_name],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            stopped = None
        killed = stopped is None or stopped.returncode != 0
        if killed:
            try:
                removed = subprocess.run(
                    [self.docker_binary, "rm", "--force", container_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return "failed", True
            if removed.returncode != 0 and "No such container" not in removed.stderr:
                return "failed", True
        return "completed", killed

    def _docker_args(self, *, container_name, container_cwd, argv, env, profile):
        args = [
            self.docker_binary,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            str(self.config.cpus),
            "--memory",
            self.config.memory,
            "--pids-limit",
            str(self.config.pids_limit),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            container_cwd,
            "--mount",
            (
                f"type=bind,source={self.workspace_root},target={CONTAINER_WORKSPACE}"
                + (",readonly" if profile.workspace_read_only else "")
            ),
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
        ]
        transient_directories = {
            ".pico": "rw,noexec,nosuid,size=32m,mode=1777",
            ".venv": "rw,nosuid,size=256m,mode=1777",
        }
        for relative, options in transient_directories.items():
            if (self.workspace_root / relative).is_dir():
                args.extend(["--tmpfs", f"{CONTAINER_WORKSPACE}/{relative}:{options}"])
        git_path = self.workspace_root / ".git"
        if git_path.exists():
            args.extend(
                [
                    "--mount",
                    f"type=bind,source={git_path},target={CONTAINER_WORKSPACE}/.git,readonly",
                ]
            )
        for secret_path in sorted(self.workspace_root.rglob(".env*")):
            if secret_path.is_file():
                relative = secret_path.relative_to(self.workspace_root).as_posix()
                args.extend(
                    [
                        "--mount",
                        f"type=bind,source=/dev/null,target={CONTAINER_WORKSPACE}/{relative},readonly",
                    ]
                )
        container_env = self._container_env(env)
        for name, value in sorted(container_env.items()):
            args.extend(["--env", f"{name}={value}"])
        args.append(self.config.image)
        args.extend(argv)
        return args

    def _container_env(self, env):
        filtered = {
            str(name): str(value)
            for name, value in dict(env).items()
            if str(name).upper() not in HOST_ENV_DENYLIST
        }
        filtered.update(
            {
                "HOME": "/tmp",
                "PATH": CONTAINER_PATH,
                "PWD": CONTAINER_WORKSPACE,
                "TIKTOKEN_CACHE_DIR": CONTAINER_TIKTOKEN_CACHE_DIR,
                "TMPDIR": "/tmp",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTEST_ADDOPTS": "-p no:cacheprovider",
                "RUFF_CACHE_DIR": "/tmp/ruff-cache",
            }
        )
        if "PYTHONPATH" not in filtered:
            filtered["PYTHONPATH"] = (
                f"{CONTAINER_WORKSPACE}/src"
                if (self.workspace_root / "src").is_dir()
                else CONTAINER_WORKSPACE
            )
        return filtered
