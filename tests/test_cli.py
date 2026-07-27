from types import SimpleNamespace

import pico.cli as cli


def test_repl_status_command_renders_status_without_starting_a_task(monkeypatch, capsys):
    agent = SimpleNamespace(
        model_client=SimpleNamespace(
            model="gpt-5.6-luna", base_url="https://api.openai.com/v1"
        ),
        session_path="/tmp/pico-session.json",
        last_prompt_metadata={},
        _last_prefix_refresh={"workspace_changed": False},
        current_task_state=None,
    )
    inputs = iter(("/status", "/exit"))

    monkeypatch.setattr(cli, "build_agent", lambda args, *, trace_sink: agent)
    monkeypatch.setattr(cli, "build_welcome", lambda *args, **kwargs: "welcome")
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    assert cli.main([]) == 0
    assert "model: gpt-5.6-luna" in capsys.readouterr().out
