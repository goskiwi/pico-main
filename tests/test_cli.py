import os
import subprocess
import sys
from unittest.mock import patch

import pico.cli as cli


def test_build_agent_uses_openai_cli_overrides(tmp_path):
    (tmp_path / ".env.local").write_text(
        "OPENAI_API_BASE=https://file.example/v1\n"
        "OPENAI_API_KEY=file-key\n"
        "OPENAI_MODEL=file-model\n",
        encoding="utf-8",
    )
    args = cli.build_arg_parser().parse_args(
        [
            "--cwd",
            str(tmp_path),
            "--model",
            "override-model",
            "--base-url",
            "https://cli.example/v1",
        ]
    )

    with patch.dict(os.environ, {}, clear=True), patch(
        "pico.cli.OpenAICompatibleModelClient"
    ) as mock_openai:
        agent = cli.build_agent(args)

    assert mock_openai.call_args.kwargs["model"] == "override-model"
    assert mock_openai.call_args.kwargs["base_url"] == "https://cli.example/v1"
    assert mock_openai.call_args.kwargs["api_key"] == "file-key"
    assert agent.model_client is mock_openai.return_value


def test_build_arg_parser_defaults_to_openai_compatible_runtime(tmp_path):
    args = cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    assert not hasattr(args, "provider")
    assert args.openai_timeout == 300


def test_build_agent_uses_official_openai_defaults(tmp_path):
    args = cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(os.environ, {}, clear=True), patch(
        "pico.cli.OpenAICompatibleModelClient"
    ) as mock_openai:
        agent = cli.build_agent(args)

    assert mock_openai.call_args.kwargs["model"] == "gpt-5.4"
    assert mock_openai.call_args.kwargs["base_url"] == "https://api.openai.com/v1"
    assert mock_openai.call_args.kwargs["api_key"] == ""
    assert agent.model_client is mock_openai.return_value


def test_build_agent_uses_workspace_env_instead_of_process_env(tmp_path):
    (tmp_path / ".env.local").write_text(
        "OPENAI_API_KEY=file-key\n"
        "OPENAI_API_BASE=https://file.example/v1\n"
        "OPENAI_MODEL=file-model\n",
        encoding="utf-8",
    )
    args = cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "shell-key",
            "OPENAI_API_BASE": "https://shell.example/v1",
            "OPENAI_MODEL": "shell-model",
        },
        clear=True,
    ), patch("pico.cli.OpenAICompatibleModelClient") as mock_openai:
        cli.build_agent(args)

    assert mock_openai.call_args.kwargs["api_key"] == "file-key"
    assert mock_openai.call_args.kwargs["base_url"] == "https://file.example/v1"
    assert mock_openai.call_args.kwargs["model"] == "file-model"


def test_build_agent_does_not_export_workspace_env(tmp_path):
    (tmp_path / ".env.local").write_text(
        "OPENAI_API_KEY=file-key\n"
        "OPENAI_API_BASE=https://file.example/v1\n"
        "PICO_TEST_NEW_VALUE=file-only\n"
        "PICO_TEST_EXISTING_VALUE=file-override\n",
        encoding="utf-8",
    )
    args = cli.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {"PICO_TEST_EXISTING_VALUE": "ambient-value"},
        clear=True,
    ), patch("pico.cli.OpenAICompatibleModelClient") as mock_openai:
        cli.build_agent(args)

        assert "PICO_TEST_NEW_VALUE" not in os.environ
        assert os.environ["PICO_TEST_EXISTING_VALUE"] == "ambient-value"
        assert "OPENAI_API_KEY" not in os.environ

    assert mock_openai.call_args.kwargs["api_key"] == "file-key"
    assert mock_openai.call_args.kwargs["base_url"] == "https://file.example/v1"


def test_cli_module_exposes_entrypoints():
    assert callable(cli.main)
    assert callable(cli.build_agent)
    assert callable(cli.build_arg_parser)


def test_module_execution_help_works():
    result = subprocess.run(
        [sys.executable, "-m", "pico", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    assert "--provider" not in result.stdout
    assert "启动中" not in result.stderr


def test_repl_reload_skills_command_calls_agent_reload(capsys):
    class FakeAgent:
        def __init__(self):
            self.model_client = type(
                "Model", (), {"model": "fake", "base_url": "https://api.example/v1"}
            )()
            self.workspace = type("Workspace", (), {"cwd": ".", "branch": "main"})()
            self.approval_policy = "auto"
            self.session = {"id": "session"}
            self.sandbox = type(
                "Sandbox",
                (),
                {"backend": "docker", "config": type("Config", (), {"image": "pico-sandbox:test"})()},
            )()
            self.session_path = ".pico/sessions/session.json"
            self.reload_called = False

        def reload_skills(self):
            self.reload_called = True
            return [object(), object()]

    fake_agent = FakeAgent()

    with patch("pico.cli.build_agent", return_value=fake_agent), patch(
        "builtins.input", side_effect=["/reload-skills", "/exit"]
    ):
        assert cli.main([]) == 0

    output = capsys.readouterr().out
    assert fake_agent.reload_called is True
    assert "skills reloaded: 2" in output
