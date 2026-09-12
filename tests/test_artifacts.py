import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pico.artifacts import ArtifactStore
from pico.run_store import RunStore


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_store = RunStore(self.root / "runs")
        self.artifacts = ArtifactStore(self.run_store, lambda text: text)
        self.run_id = "run_test"

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, content):
        return self.artifacts.write_tool_output(
            self.run_id,
            "call_test",
            content,
        )["artifact_id"]

    def test_read_slice_reads_only_one_bounded_page_after_verification(self):
        artifact_id = self._write("x" * (2 * 1024 * 1024))
        original_open = Path.open
        read_sizes = []

        class RecordingReader:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            def __enter__(self):
                self.wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self.wrapped.__exit__(*args)

            def read(self, size=-1):
                read_sizes.append(size)
                return self.wrapped.read(size)

            def seek(self, offset):
                return self.wrapped.seek(offset)

        def recording_open(path, *args, **kwargs):
            opened = original_open(path, *args, **kwargs)
            if path.suffix == ".txt" and args and args[0] == "rb":
                return RecordingReader(opened)
            return opened

        with mock.patch.object(Path, "open", recording_open):
            page = self.artifacts.read_slice(self.run_id, artifact_id, 0, 8192)

        self.assertEqual(len(page["content"]), 8192)
        self.assertEqual(page["total_bytes"], 2 * 1024 * 1024)
        self.assertNotIn(-1, read_sizes)
        self.assertLessEqual(max(read_sizes), 1024 * 1024)

    def test_verified_source_cache_keeps_metadata_not_artifact_bytes(self):
        artifact_id = self._write("x" * (2 * 1024 * 1024))

        self.artifacts.read_slice(self.run_id, artifact_id, 0, 16)

        cached = self.artifacts._verified_source
        self.assertIsInstance(cached, tuple)
        self.assertFalse(any(isinstance(item, bytes) for item in cached))

    def test_read_slice_preserves_utf8_boundaries(self):
        artifact_id = self._write("A你B好C")

        first = self.artifacts.read_slice(self.run_id, artifact_id, 0, 4)
        second = self.artifacts.read_slice(
            self.run_id,
            artifact_id,
            first["end_offset"],
            4,
        )
        third = self.artifacts.read_slice(
            self.run_id,
            artifact_id,
            second["end_offset"],
            4,
        )

        self.assertEqual(first["content"] + second["content"] + third["content"], "A你B好C")

    def test_changed_artifact_invalidates_verified_cache(self):
        artifact_id = self._write("original")
        self.artifacts.read_slice(self.run_id, artifact_id, 0, 8)
        content_path = self.run_store.artifact_dir(self.run_id) / f"{artifact_id}.txt"
        content_path.write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "artifact digest mismatch"):
            self.artifacts.read_slice(self.run_id, artifact_id, 0, 8)


if __name__ == "__main__":
    unittest.main()
