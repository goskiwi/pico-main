import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico.execution import ExecutionContext
from pico.tool_context import ToolContext
from pico.tools import tool_read_file


class ReadFileTests(unittest.TestCase):
    def test_range_read_stops_after_one_lookahead_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "large.txt"
            target.write_text("".join(f"line-{index}\n" for index in range(10_000)))
            original_open = Path.open
            read_calls = 0

            class RecordingReader:
                def __init__(self, wrapped):
                    self.wrapped = wrapped

                def __enter__(self):
                    self.wrapped.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.wrapped.__exit__(*args)

                def readline(self, size=-1):
                    nonlocal read_calls
                    read_calls += 1
                    return self.wrapped.readline(size)

            def recording_open(path, *args, **kwargs):
                opened = original_open(path, *args, **kwargs)
                return RecordingReader(opened) if path == target else opened

            context = ToolContext(
                run_id="run_test",
                tool_call_id="read_test",
                execution_context=ExecutionContext.root(max_seconds=2),
            )
            with mock.patch.object(Path, "open", recording_open):
                result = tool_read_file(
                    context,
                    {"path": "large.txt", "start_line": 1, "end_line": 2},
                    path_resolver=lambda value: root / value,
                    workspace_root=root,
                )

            self.assertEqual(read_calls, 3)
            self.assertTrue(result.structured["has_more"])
            self.assertNotIn("revision", result.structured)
            self.assertNotIn("total_lines", result.structured)


if __name__ == "__main__":
    unittest.main()
