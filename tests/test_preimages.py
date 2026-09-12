import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pico.artifacts import ArtifactStore
from pico.contracts import ToolCall
from pico.evidence import RunChangeSet
from pico.run_store import RunStore
from pico.tool_runtime import ToolRuntime


class PreimageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_store = RunStore(self.root / "runs")
        self.artifacts = ArtifactStore(self.run_store, lambda text: text)
        self.change_set = RunChangeSet()
        self.agent = SimpleNamespace(
            run=SimpleNamespace(
                run_log=SimpleNamespace(run_id="run_test"),
                evidence=SimpleNamespace(change_set=self.change_set),
            ),
            dependencies=SimpleNamespace(artifacts=self.artifacts),
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _revision(content):
        return "sha256:" + hashlib.sha256(content).hexdigest()

    def test_existing_run_change_does_not_write_another_full_preimage(self):
        target = self.root / "tracked.txt"
        original = b"original\n"
        target.write_bytes(original)
        logical = "tracked.txt"

        first = ToolRuntime._preimage_artifacts(
            self.agent,
            ToolCall("edit_file", {}, "call_1"),
            ((logical, target),),
            {logical: self._revision(original)},
            "workspace",
        )
        self.assertTrue(first[logical].startswith("preimage_"))

        self.change_set.apply_effect(
            {
                "effect_scope": "workspace",
                "side_effect_state": "changed",
                "affected_paths": (logical,),
                "event_sequence": 1,
                "path_transitions": (
                    {
                        "path": logical,
                        "before_state": self._revision(original),
                        "after_state": "sha256:first-edit",
                        "before_artifact_id": first[logical],
                    },
                ),
            }
        )
        target.write_bytes(b"first edit\n")

        second = ToolRuntime._preimage_artifacts(
            self.agent,
            ToolCall("edit_file", {}, "call_2"),
            ((logical, target),),
            {logical: self._revision(b"first edit\n")},
            "workspace",
        )

        self.assertEqual(second, {})
        artifact_dir = self.run_store.artifact_dir("run_test")
        self.assertEqual(len(list(artifact_dir.glob("preimage_*.txt"))), 1)

    def test_later_transition_keeps_the_first_preimage(self):
        logical = "tracked.txt"
        self.change_set.apply_effect(
            {
                "effect_scope": "workspace",
                "side_effect_state": "changed",
                "affected_paths": (logical,),
                "event_sequence": 1,
                "path_transitions": (
                    {
                        "path": logical,
                        "before_state": "sha256:original",
                        "after_state": "sha256:first-edit",
                        "before_artifact_id": "preimage_first",
                    },
                ),
            }
        )
        self.change_set.apply_effect(
            {
                "effect_scope": "workspace",
                "side_effect_state": "changed",
                "affected_paths": (logical,),
                "event_sequence": 2,
                "path_transitions": (
                    {
                        "path": logical,
                        "before_state": "sha256:first-edit",
                        "after_state": "sha256:second-edit",
                        "before_artifact_id": "",
                    },
                ),
            }
        )

        change = self.change_set.files[logical]
        self.assertEqual(change.first_before_artifact_id, "preimage_first")
        self.assertEqual(change.current_after_state, "sha256:second-edit")


if __name__ == "__main__":
    unittest.main()
