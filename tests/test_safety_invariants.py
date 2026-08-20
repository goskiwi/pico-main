import os
from unittest.mock import patch

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.sandbox import (
    DockerSandbox,
    SandboxProfile,
    SandboxResult,
    parse_command_invocation,
)


class FakeSandbox:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return SandboxResult(returncode=0, stdout="sandbox-ok\n")

def build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n")
    sandbox = kwargs.pop("sandbox", None)
    return Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path),
                SessionStore(tmp_path / ".pico/sessions"),
                config=PicoConfig(approval_policy="auto", verification_command=""),
                sandbox=sandbox, **kwargs)


def test_workspace_and_symlink_escape_are_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("secret")
    agent = build_agent(tmp_path)
    assert agent.tools.run("read_file", {"path": "../" + outside.name}).status == "rejected"
    (tmp_path / "link").symlink_to(outside)
    assert agent.tools.run("read_file", {"path": "link"}).status == "rejected"


def test_shell_is_direct_argv_in_docker_and_env_is_filtered(tmp_path):
    sandbox = FakeSandbox()
    agent = build_agent(tmp_path, sandbox=sandbox)
    with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "LANG": "C"}, clear=True):
        outcome = agent.tools.run(
            "run_shell", {"command": "python -c 'print(1)'", "timeout": 3}
        )
    assert outcome.status == "ok"
    argv, options = sandbox.calls[0]
    assert argv == ("python", "-c", "print(1)")
    assert "OPENAI_API_KEY" not in options["env"]
    assert options["timeout"] == 3
    assert options["profile"] == SandboxProfile.INSPECT


def test_shell_parser_does_not_invoke_a_host_shell():
    argv, env = parse_command_invocation("MODE=test python -m pytest -q")
    assert argv == ("python", "-m", "pytest", "-q")
    assert env == {"MODE": "test"}


def test_approval_denial_prevents_sandbox_start(tmp_path):
    sandbox = FakeSandbox()
    agent = build_agent(tmp_path, sandbox=sandbox)
    agent.config = PicoConfig.build(agent.config, approval_policy="never")
    outcome = agent.tools.run("run_shell", {"command": "echo hi"})
    assert outcome.status == "rejected"
    assert sandbox.calls == []


def test_docker_profiles_mount_workspace_read_only(tmp_path):
    (tmp_path / "src").mkdir()
    sandbox = DockerSandbox(tmp_path, docker_binary="docker")
    args = sandbox._docker_args(
        container_name="pico-test",
        container_cwd="/workspace",
        argv=("pytest", "-q"),
        env={},
        profile=SandboxProfile.VERIFY,
    )
    mount = args[args.index("--mount") + 1]
    assert "target=/workspace,readonly" in mount
    assert "--read-only" in args
    assert not any("/workspace/.venv:" in item for item in args)
    assert "PYTHONPATH=/workspace/src" in args
    assert "PYTHONNOUSERSITE=1" in args


def test_explicit_pythonpath_is_not_overwritten(tmp_path):
    sandbox = DockerSandbox(tmp_path, docker_binary="docker")
    args = sandbox._docker_args(
        container_name="pico-test",
        container_cwd="/workspace",
        argv=("python", "-c", "pass"),
        env={"PYTHONPATH": "/workspace/custom"},
        profile=SandboxProfile.INSPECT,
    )

    assert "PYTHONPATH=/workspace/custom" in args
    assert args.count("PYTHONPATH=/workspace/custom") == 1
