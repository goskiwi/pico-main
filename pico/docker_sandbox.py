"""Session-owned Docker execution; no host-shell fallback."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path

from .command_runner import CommandResult, CommandRunner
from .execution import ExecutionCancelled, ExecutionContext, ExecutionDeadlineExceeded
from .persistence import atomic_write_json
from .workspace import _is_protected_environment_name

DEFAULT_DOCKER_IMAGE = "pico-sandbox:python"
DOCKER_CONTROL_TIMEOUT = 10
CONTAINER_NAME = re.compile(r"^pico-[a-f0-9]{32}$")
HIDDEN_DIRECTORIES = frozenset({".pico", ".venv", "venv"})


class DockerSandbox:
    """One reusable container per ask attempt, mounted onto the live workspace.

    Only model-requested commands enter this container. A tiny ownership record
    is written before creation so a subsequent process can remove an interrupted
    attempt's container before observing or resuming its Run.
    """

    def __init__(self, workspace_root, *, session, image=DEFAULT_DOCKER_IMAGE):
        self.root = Path(workspace_root).resolve()
        self.image = str(image).strip()
        if (
            not self.image
            or self.image.startswith("-")
            or any(char.isspace() for char in self.image)
        ):
            raise ValueError("Docker image must be a non-empty image reference")
        self.record = session.path.parent / "sandbox.json"
        self.labels = {
            "pico.sandbox": "1",
            "pico.session": session.id,
            "pico.workspace": str(self.root),
        }
        self.host = CommandRunner(self.root)
        self._name = ""
        self._scratch = None

    @property
    def execution_policy(self):
        return {
            "executor": "docker",
            "image": self.image,
            "cwd": "/workspace",
            "network": "none",
            "environment_policy": "container-minimal",
        }

    @staticmethod
    def _docker_environment():
        # Docker's host-side client needs its context configuration. These
        # variables are NOT passed to docker exec or the container.
        environment = {
            "DOCKER_CONFIG": os.environ.get(
                "DOCKER_CONFIG", str(Path.home() / ".docker")
            ),
        }
        for name in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        ):
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment

    def _control(self, args, *, context=None):
        executable = shutil.which("docker")
        if executable is None:
            raise RuntimeError("Docker is not installed; run_shell cannot use the host")
        result = self.host.run(
            (executable, *args),
            cwd=self.root,
            timeout=DOCKER_CONTROL_TIMEOUT,
            env=self._docker_environment(),
            execution_context=context,
        )
        if context is not None:
            context.check_active()
        return result

    @staticmethod
    def _require_success(result, operation):
        if result.infrastructure_error or result.stop_reason or result.returncode != 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or result.stop_reason
            )
            raise RuntimeError(f"Docker {operation} failed: {detail}")
        return result.stdout.strip()

    def _read_record(self):
        if self.record.is_symlink():
            raise RuntimeError("Docker ownership record must not be a symlink")
        if not self.record.exists():
            return ""
        value = json.loads(self.record.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != {"container"}
            or not (
                isinstance(value["container"], str)
                and CONTAINER_NAME.fullmatch(value["container"])
            )
        ):
            raise RuntimeError("invalid Docker ownership record")
        return value["container"]

    def reconcile(self):
        """Dispose a previous attempt; never silently resume its commands."""
        if self._read_record():
            self.close()

    def close(self):
        name = self._name or self._read_record()
        if not name:
            if self._scratch is not None:
                self._scratch.cleanup()
                self._scratch = None
            return
        inspected = self._control(("container", "inspect", name))
        if inspected.returncode != 0:
            if (
                "No such container" not in inspected.stderr
                and "No such object" not in inspected.stderr
            ):
                self._require_success(inspected, "inspect during cleanup")
        else:
            info = json.loads(inspected.stdout)[0]
            actual = info["Config"].get("Labels") or {}
            if any(actual.get(key) != value for key, value in self.labels.items()):
                raise RuntimeError(
                    "Docker container ownership mismatch; refusing cleanup"
                )
            self._require_success(
                self._control(("stop", "--timeout", "1", name)), "stop"
            )
            self._require_success(self._control(("rm", "--volumes", name)), "remove")
        self.record.unlink(missing_ok=True)
        self._name = ""
        if self._scratch is not None:
            self._scratch.cleanup()
            self._scratch = None

    def _protected_paths(self, context):
        masks = []
        for directory, directories, files in os.walk(self.root, followlinks=False):
            context.check_active()
            for name in (*directories, *files):
                path = Path(directory) / name
                kind = None
                if name.casefold() == ".git":
                    kind = "readonly"
                elif (
                    name.casefold() in HIDDEN_DIRECTORIES
                    or _is_protected_environment_name(name)
                ):
                    kind = "hidden"
                elif name in files:
                    mode = path.lstat().st_mode
                    if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
                        kind = "hidden"
                if kind is None:
                    continue
                if path.is_symlink():
                    raise ValueError(
                        f"protected sandbox path must not be a symlink: {path.relative_to(self.root)}"
                    )
                masks.append(
                    (kind, path.relative_to(self.root).as_posix(), path.is_dir())
                )
                if name in directories:
                    directories.remove(name)
        return tuple(sorted(masks))

    @staticmethod
    def _mount(source, target, *, readonly=False):
        if any(char in str(source) + str(target) for char in (",", "\n")):
            raise ValueError("Docker bind paths cannot contain commas or newlines")
        return f"type=bind,src={source},dst={target}" + (
            ",readonly" if readonly else ""
        )

    def _create_args(self, name, masks, empty_file):
        user_id = os.getuid() or 1000
        group_id = os.getgid() or 1000
        args = [
            "create",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--init",
            "--memory=1g",
            "--memory-swap=1g",
            "--cpus=2",
            "--pids-limit=128",
            "--user",
            f"{user_id}:{group_id}",
            "--workdir",
            "/workspace",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "UV_CACHE_DIR=/tmp/uv-cache",
            "--env",
            "UV_PROJECT_ENVIRONMENT=/tmp/venv",
            "--mount",
            self._mount(self.root, "/workspace"),
        ]
        for key, value in self.labels.items():
            args.extend(("--label", f"{key}={value}"))
        for kind, relative, directory in masks:
            target = f"/workspace/{relative}"
            if kind == "hidden" and directory:
                args.extend(
                    (
                        "--mount",
                        f"type=tmpfs,dst={target},tmpfs-size=1048576,tmpfs-mode=0555",
                    )
                )
            else:
                source = self.root / relative if kind == "readonly" else empty_file
                args.extend(("--mount", self._mount(source, target, readonly=True)))
        args.extend(
            ("--entrypoint", "/bin/sh", self.image, "-c", "exec sleep infinity")
        )
        return tuple(args)

    def _ensure(self, context):
        if self._name:
            return
        masks = self._protected_paths(context)
        endpoint = os.environ.get("DOCKER_HOST") or self._require_success(
            self._control(
                ("context", "inspect", "--format", "{{.Endpoints.docker.Host}}"),
                context=context,
            ),
            "context inspection",
        )
        if not endpoint.startswith("unix://"):
            raise RuntimeError(
                "Pico requires a local Unix-socket Docker engine for workspace bind mounts"
            )
        operating_system = self._require_success(
            self._control(("info", "--format", "{{.OSType}}"), context=context),
            "engine check",
        )
        if operating_system != "linux":
            raise RuntimeError("Pico requires Linux containers")
        self._require_success(
            self._control(("image", "inspect", self.image), context=context),
            f"image lookup ({self.image}); build the sandbox image first",
        )
        name = "pico-" + uuid.uuid4().hex
        self._scratch = tempfile.TemporaryDirectory(prefix="pico-docker-")
        empty_file = Path(self._scratch.name) / "empty"
        empty_file.touch(mode=0o444)
        if self.record.is_symlink():
            raise RuntimeError("Docker ownership record must not be a symlink")
        atomic_write_json(self.record, {"container": name})
        self._name = name
        self._require_success(
            self._control(self._create_args(name, masks, empty_file), context=context),
            "create",
        )
        self._require_success(self._control(("start", name), context=context), "start")

    def run(self, argv, *, cwd, timeout, env=None, execution_context=None):
        if env:
            raise ValueError("run_shell cannot inject host environment variables")
        relative = Path(cwd).resolve().relative_to(self.root)
        target = (
            "/workspace"
            if relative == Path(".")
            else "/workspace/" + relative.as_posix()
        )
        parent = execution_context or ExecutionContext.standalone(max_seconds=timeout)
        context = ExecutionContext.root(
            max_seconds=timeout,
            token=parent.token,
            deadline=time.monotonic() + parent.bounded_timeout(timeout),
        )
        try:
            self._ensure(context)
        except (ExecutionCancelled, ExecutionDeadlineExceeded):
            self.close()
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            self.close()
            return CommandResult(None, stderr=str(exc), infrastructure_error=True)
        try:
            result = self.host.run(
                (
                    shutil.which("docker"),
                    "exec",
                    "--workdir",
                    target,
                    self._name,
                    *argv,
                ),
                cwd=self.root,
                timeout=timeout,
                env=self._docker_environment(),
                execution_context=context,
            )
        except BaseException:
            self.close()
            raise
        if result.stop_reason or result.infrastructure_error:
            self.close()
        return result
