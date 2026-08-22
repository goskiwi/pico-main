import os
from unittest.mock import patch

from pico import FakeModelClient, Pico, PicoConfig, SessionStore, WorkspaceContext
from pico.sandbox import (
    DockerSandbox,
    SandboxResult,
    parse_command_invocation,
)


class FakeSandbox:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or SandboxResult(returncode=0, stdout="sandbox-ok\n")

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.result

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


def test_file_tools_reject_git_and_pico_internal_paths(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("internal\n", encoding="utf-8")
    agent = build_agent(tmp_path)

    read_git = agent.tools.run(
        "read_file",
        {"path": ".git/config", "start": 1, "end": 10},
    )
    write_pico = agent.tools.run(
        "write_file",
        {
            "path": ".pico/injected.txt",
            "content": "injected\n",
            "expected_revision": "absent",
        },
    )
    write_gitignore = agent.tools.run(
        "write_file",
        {
            "path": ".gitignore",
            "content": ".pico/\n",
            "expected_revision": "absent",
        },
    )

    assert read_git.status == "rejected"
    assert write_pico.status == "rejected"
    assert not (tmp_path / ".pico" / "injected.txt").exists()
    assert write_gitignore.status == "success"


def test_shell_is_direct_argv_in_docker_and_env_is_filtered(tmp_path):
    sandbox = FakeSandbox()
    agent = build_agent(tmp_path, sandbox=sandbox)
    with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "LANG": "C"}, clear=True):
        outcome = agent.tools.run(
            "run_shell", {"command": "python -c 'print(1)'", "timeout": 3}
        )
    assert outcome.status == "success"
    argv, options = sandbox.calls[0]
    assert argv == ("python", "-c", "print(1)")
    assert "OPENAI_API_KEY" not in options["env"]
    assert options["timeout"] == 3


def test_shell_failure_is_structured_before_tool_executor_classification(tmp_path):
    sandbox = FakeSandbox(
        SandboxResult(returncode=7, stderr="command failed\n")
    )
    agent = build_agent(tmp_path, sandbox=sandbox)

    outcome = agent.tools.run(
        "run_shell", {"command": "false", "timeout": 3}
    )

    assert outcome.status == "error"
    assert outcome.execution_state == "completed"
    assert outcome.side_effect_state == "none"
    assert outcome.failure.code == "command_failed"
    assert outcome.failure.detail == "command exited with 7"


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


def test_docker_mounts_workspace_read_only(tmp_path):
    (tmp_path / "src").mkdir()
    sandbox = DockerSandbox(tmp_path, docker_binary="docker")
    args = sandbox._docker_args(
        container_name="pico-test",
        container_cwd="/workspace",
        argv=("pytest", "-q"),
        env={},
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
    )

    assert "PYTHONPATH=/workspace/custom" in args
    assert args.count("PYTHONPATH=/workspace/custom") == 1
