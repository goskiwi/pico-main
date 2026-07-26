import json
from io import StringIO
from types import SimpleNamespace

import pico.cli as cli
from pico.trace_events import TraceSink


def test_terminal_trace_sink_renders_compact_live_events():
    stream = StringIO()
    sink = TraceSink("terminal", stream)

    sink.emit(
        {
            "event": "tool_end",
            "elapsed_ms": 1_230,
            "tool": "read_file",
            "status": "ok",
            "duration_ms": 18,
        }
    )

    assert stream.getvalue() == "[00:01.23] tool_end tool=read_file status=ok duration=18ms\n"


def test_jsonl_trace_sink_emits_one_parseable_event_per_line():
    stream = StringIO()
    sink = TraceSink("jsonl", stream)
    event = {"event": "run_end", "run_id": "run_demo", "seq": 4}

    sink.emit(event)

    assert [json.loads(line) for line in stream.getvalue().splitlines()] == [event]


def test_cli_exposes_human_and_jsonl_live_trace_modes():
    parser = cli.build_arg_parser()

    assert parser.parse_args(["--trace", "inspect", "README.md"]).trace is True
    assert parser.parse_args(["--trace-jsonl", "-", "inspect"]).trace_jsonl == "-"


def test_cli_jsonl_stdout_stays_machine_readable(monkeypatch, capsys):
    class FakeAgent:
        model_client = SimpleNamespace(model="fake", base_url="test://fake")

        def __init__(self, trace_sink):
            self.trace_sink = trace_sink

        def ask(self, prompt):
            assert prompt == "inspect"
            self.trace_sink.emit({"event": "run_end", "run_id": "run_demo", "seq": 1})
            return "finished"

    monkeypatch.setattr(
        cli,
        "build_agent",
        lambda args, *, trace_sink: FakeAgent(trace_sink),
    )
    monkeypatch.setattr(cli, "build_welcome", lambda *args, **kwargs: "welcome")

    assert cli.main(["--trace-jsonl", "-", "inspect"]) == 0

    captured = capsys.readouterr()
    assert [json.loads(line) for line in captured.out.splitlines()] == [
        {"event": "run_end", "run_id": "run_demo", "seq": 1}
    ]
    assert "welcome" in captured.err
    assert "finished" in captured.err
