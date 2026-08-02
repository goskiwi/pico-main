"""Mandatory Docker isolation for model-requested shell commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SANDBOX_IMAGE = "pico/sandbox:latest"
DEFAULT_SANDBOX_CPUS = 4.0
DEFAULT_SANDBOX_MEMORY = "4g"
DEFAULT_SANDBOX_PIDS_LIMIT = 512
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
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class DockerSandboxConfig:
    image: str = DEFAULT_SANDBOX_IMAGE
    cpus: float = DEFAULT_SANDBOX_CPUS
    memory: str = DEFAULT_SANDBOX_MEMORY
    pids_limit: int = DEFAULT_SANDBOX_PIDS_LIMIT

    def __post_init__(self):
        if not str(self.image).strip():
            raise ValueError("sandbox image must not be empty")
        if float(self.cpus) <= 0:
            raise ValueError("sandbox cpus must be greater than zero")
        if not str(self.memory).strip():
            raise ValueError("sandbox memory must not be empty")
        if int(self.pids_limit) < 16:
            raise ValueError("sandbox pids_limit must be at least 16")


class DockerSandbox:
    """Run commands in an ephemeral, resource-limited Docker container."""

    backend = "docker"

    def __init__(self, workspace_root, config=None, docker_binary=None):
        self.workspace_root = Path(workspace_root).resolve()
        self.config = config or DockerSandboxConfig()
        self.docker_binary = str(docker_binary or shutil.which("docker") or "docker")

    def identity(self):
        return {
            "backend": self.backend,
            "image": self.config.image,
            "cpus": float(self.config.cpus),
            "memory": self.config.memory,
            "pids_limit": int(self.config.pids_limit),
            "network": "none",
            "rootfs_read_only": True,
        }

    def audit_metadata(self, *, timed_out=False):
        return {
            "sandbox_backend": self.backend,
            "sandbox_image": self.config.image,
            "sandbox_network": "none",
            "sandbox_rootfs_read_only": True,
            "sandbox_cpus": float(self.config.cpus),
            "sandbox_memory": self.config.memory,
            "sandbox_pids_limit": int(self.config.pids_limit),
            "sandbox_timed_out": bool(timed_out),
        }

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
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailableError(f"Docker readiness check failed: {exc}") from exc

    def run(self, command, *, cwd, timeout, env=None):
        self.ensure_ready()
        cwd = Path(cwd).resolve()
        try:
            relative_cwd = cwd.relative_to(self.workspace_root)
        except ValueError as exc:
            raise SandboxError(f"sandbox cwd escapes workspace: {cwd}") from exc

        container_name = "pico-" + uuid.uuid4().hex[:12]
        container_cwd = Path(CONTAINER_WORKSPACE, relative_cwd).as_posix()
        docker_args = self._docker_args(
            container_name=container_name,
            container_cwd=container_cwd,
            command=str(command),
            env=env or {},
        )
        try:
            process = subprocess.Popen(
                docker_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            raise SandboxUnavailableError(f"Could not start Docker sandbox: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=int(timeout))
            return SandboxResult(
                returncode=int(process.returncode or 0),
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                [self.docker_binary, "rm", "--force", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            stdout, stderr = process.communicate(timeout=5)
            timeout_message = f"sandbox command timed out after {int(timeout)} seconds"
            stderr = "\n".join(part for part in (stderr.strip(), timeout_message) if part)
            return SandboxResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )

    def _docker_args(self, *, container_name, container_cwd, command, env):
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
            f"type=bind,source={self.workspace_root},target={CONTAINER_WORKSPACE}",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--tmpfs",
            "/workspace/.pico:rw,noexec,nosuid,size=32m",
            "--tmpfs",
            "/workspace/.venv:rw,nosuid,size=256m",
        ]
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
        args.extend([self.config.image, "/bin/sh", "-lc", command])
        return args

    @staticmethod
    def _container_env(env):
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
            }
        )
        return filtered
