import os
import subprocess
from unittest.mock import patch

import pytest

from pico.sandbox import (
    CONTAINER_PATH,
    DockerSandbox,
    DockerSandboxConfig,
    SandboxImageMissingError,
    SandboxResult,
)
from tests.helpers import UnitTestSandbox, build_agent


def test_default_sandbox_resources_fit_regular_repository_workloads():
    config = DockerSandboxConfig()

    assert config.cpus == 4.0
    assert config.memory == "4g"
    assert config.pids_limit == 512


def test_docker_command_enforces_isolation_and_resource_limits(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / ".env.local").write_text("TOKEN=secret\n", encoding="utf-8")
    sandbox = DockerSandbox(
        tmp_path,
        config=DockerSandboxConfig(
            image="pico-sandbox:test",
            cpus=0.5,
            memory="256m",
            pids_limit=64,
        ),
        docker_binary="/usr/bin/docker",
    )

    args = sandbox._docker_args(
        container_name="pico-test",
        container_cwd="/workspace",
        command="pytest -q",
        env={"LANG": "C.UTF-8", "PATH": "/host/bin", "HOME": "/Users/test"},
    )

    assert args[:5] == ["/usr/bin/docker", "run", "--rm", "--pull", "never"]
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert args[args.index("--cpus") + 1] == "0.5"
    assert args[args.index("--memory") + 1] == "256m"
    assert args[args.index("--pids-limit") + 1] == "64"
    assert any("target=/workspace/.git,readonly" in value for value in args)
    assert any("source=/dev/null,target=/workspace/.env,readonly" in value for value in args)
    assert any("source=/dev/null,target=/workspace/service/.env.local,readonly" in value for value in args)
    assert f"PATH={CONTAINER_PATH}" in args
    assert "HOME=/tmp" in args
    assert "PATH=/host/bin" not in args
    assert args[-4:] == ["pico-sandbox:test", "/bin/sh", "-lc", "pytest -q"]


def test_docker_sandbox_rejects_missing_prebuilt_image(tmp_path):
    sandbox = DockerSandbox(
        tmp_path,
        config=DockerSandboxConfig(image="pico-sandbox:missing"),
        docker_binary="docker",
    )
    version = subprocess.CompletedProcess(["docker", "version"], 0, "29.0", "")
    missing = subprocess.CompletedProcess(["docker", "image", "inspect"], 1, "", "not found")

    with patch("pico.sandbox.shutil.which", return_value="/usr/bin/docker"), patch(
        "pico.sandbox.subprocess.run",
        side_effect=[version, missing],
    ):
        with pytest.raises(SandboxImageMissingError) as exc_info:
            sandbox.ensure_ready()

    assert "docker build -f Dockerfile.sandbox" in str(exc_info.value)


def test_docker_sandbox_timeout_force_removes_container(tmp_path):
    sandbox = DockerSandbox(tmp_path, docker_binary="docker")

    class TimedOutProcess:
        returncode = -9

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("docker", timeout)
            return "partial output\n", ""

    process = TimedOutProcess()
    with patch.object(sandbox, "ensure_ready"), patch(
        "pico.sandbox.subprocess.Popen",
        return_value=process,
    ), patch("pico.sandbox.subprocess.run") as remove:
        result = sandbox.run("sleep 30", cwd=tmp_path, timeout=1, env={})

    assert result.returncode == 124
    assert result.timed_out is True
    assert "timed out after 1 seconds" in result.stderr
    remove.assert_called_once()
    remove_args = remove.call_args.args[0]
    assert remove_args[:3] == ["docker", "rm", "--force"]


def test_run_shell_does_not_fall_back_when_sandbox_is_unavailable(tmp_path):
    class MissingSandbox(UnitTestSandbox):
        backend = "docker"

        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout, env
            raise SandboxImageMissingError("sandbox image is missing")

    sandbox = MissingSandbox(tmp_path)
    sandbox.config.image = "pico-sandbox:missing"
    agent = build_agent(tmp_path, [], sandbox=sandbox)

    result = agent.run_tool("run_shell", {"command": "echo must-not-run", "timeout": 10})

    assert "sandbox image is missing" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "sandbox_image_missing"
    assert agent._last_tool_result_metadata["security_event_type"] == "sandbox_image_missing"


def test_run_shell_timeout_has_stable_audit_status(tmp_path):
    class TimeoutSandbox(UnitTestSandbox):
        backend = "docker"

        def run(self, command, *, cwd, timeout, env=None):
            del command, cwd, timeout, env
            return SandboxResult(
                returncode=124,
                stderr="sandbox command timed out",
                timed_out=True,
            )

    agent = build_agent(tmp_path, [], sandbox=TimeoutSandbox(tmp_path))

    result = agent.run_tool("run_shell", {"command": "sleep 30", "timeout": 1})

    assert "exit_code: 124" in result
    assert agent._last_tool_result_metadata["tool_error_code"] == "sandbox_timeout"
    assert agent._last_tool_result_metadata["security_event_type"] == "sandbox_timeout"
    assert agent._last_tool_result_metadata["sandbox_timed_out"] is True


@pytest.mark.skipif(
    os.environ.get("PICO_RUN_DOCKER_TESTS") != "1",
    reason="set PICO_RUN_DOCKER_TESTS=1 after building the sandbox image",
)
def test_docker_sandbox_real_container_smoke(tmp_path):
    sandbox = DockerSandbox(tmp_path)

    result = sandbox.run("printf 'isolated'", cwd=tmp_path, timeout=10, env={})

    assert result.returncode == 0
    assert result.stdout == "isolated"


@pytest.mark.skipif(
    os.environ.get("PICO_RUN_DOCKER_TESTS") != "1",
    reason="set PICO_RUN_DOCKER_TESTS=1 after building the sandbox image",
)
def test_docker_sandbox_real_isolation_boundaries(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=must-not-be-visible\n", encoding="utf-8")
    sandbox = DockerSandbox(tmp_path)

    hidden_env = sandbox.run("wc -c < .env", cwd=tmp_path, timeout=10, env={})
    read_only_root = sandbox.run("touch /pico-host-escape", cwd=tmp_path, timeout=10, env={})
    network = sandbox.run(
        "python -c \"import socket; s=socket.socket(); s.settimeout(1); "
        "r=s.connect_ex(('1.1.1.1', 53)); print(r); raise SystemExit(0 if r else 1)\"",
        cwd=tmp_path,
        timeout=10,
        env={},
    )
    workspace_write = sandbox.run(
        "printf 'sandbox-write' > result.txt",
        cwd=tmp_path,
        timeout=10,
        env={},
    )

    assert hidden_env.returncode == 0
    assert hidden_env.stdout.strip() == "0"
    assert "must-not-be-visible" not in hidden_env.stdout + hidden_env.stderr
    assert read_only_root.returncode != 0
    assert network.returncode == 0
    assert workspace_write.returncode == 0
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "sandbox-write"


@pytest.mark.skipif(
    os.environ.get("PICO_RUN_DOCKER_TESTS") != "1",
    reason="set PICO_RUN_DOCKER_TESTS=1 after building the sandbox image",
)
def test_docker_sandbox_real_resource_limits_and_timeout(tmp_path):
    sandbox = DockerSandbox(tmp_path)

    limits = sandbox.run(
        "cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max",
        cwd=tmp_path,
        timeout=10,
        env={},
    )
    timed_out = sandbox.run("sleep 10", cwd=tmp_path, timeout=1, env={})

    assert limits.returncode == 0
    cpu_line, memory_line, pids_line = limits.stdout.splitlines()
    cpu_quota, cpu_period = (int(value) for value in cpu_line.split())
    assert cpu_quota / cpu_period == 4.0
    assert int(memory_line) == 4 * 1024 * 1024 * 1024
    assert int(pids_line) == 512
    assert timed_out.returncode == 124
    assert timed_out.timed_out is True
