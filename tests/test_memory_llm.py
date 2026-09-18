"""Opt-in real extraction, cross-Session recall, correction and explicit deletion."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pico import Pico, PicoConfig, SessionStore, Workspace
from pico.cli import _build_model_client
from pico.env import load_project_env


@unittest.skipUnless(os.environ.get("PICO_MEMORY_LLM_TEST") == "1", "opt-in real memory model test")
class RealMemoryTests(unittest.TestCase):
    def test_extract_recall_correct_and_forget(self):
        project = Path(__file__).resolve().parents[1]
        load_project_env(project, boundary=project)
        self.assertTrue(os.environ.get("PICO_OPENAI_API_KEY"), "PICO_OPENAI_API_KEY is required")
        with tempfile.TemporaryDirectory(prefix="pico-memory-llm-") as directory:
            root = Path(directory)
            workspace = Workspace.build(root, repo_root_override=root)
            sessions = SessionStore(root / ".pico" / "sessions")
            config = PicoConfig(memory_enabled=True, mode="ask", context_limit_tokens=64000,
                                max_output_tokens=2000, recent_history_tokens=2000)
            first = Pico(_build_model_client(SimpleNamespace(model=None)), workspace, sessions.create(root), config=config)
            self.addCleanup(first.close)
            first.remember("请记住：用户偏好使用 pytest 编写 Python 回归测试，而不是 unittest。这是测试工具偏好，不是这次任务的临时状态。")
            worker = first.dependencies.memory_worker
            self.assertTrue(worker.idle.wait(60), "memory worker did not finish")
            store = first.dependencies.memory_store
            entries = store.catalog()["entries"]
            self.assertTrue(entries, worker.last_error)
            topic_name = next(item["filename"] for item in entries if "pytest" in store.read(item["filename"])["content"])
            first.close()

            second = Pico(_build_model_client(SimpleNamespace(model=None)), workspace, sessions.create(root), config=config)
            self.addCleanup(second.close)
            result = second.ask("根据项目记忆，这个用户偏好用什么 Python 测试工具？先调用 list_memories 和 read_memory 查证，再回答。不要运行 Shell 或修改文件。")
            self.assertEqual(result.status, "completed")
            self.assertIn("pytest", result.answer.lower())
            events = second.read_run_events(result.run_id)
            self.assertTrue(any(event.kind == "tool_result" and event.payload["outcome"]["tool_name"] == "read_memory"
                                and event.payload["outcome"]["status"] == "success" for event in events))
            self.assertTrue(second.dependencies.memory_worker.idle.wait(60))
            second.remember("纠正之前的测试工具偏好：现在用户改为偏好 unittest，不再偏好 pytest。请更新原有主题，不要新增一份矛盾或重复的测试偏好。")
            self.assertTrue(second.dependencies.memory_worker.idle.wait(60))
            updated = store.read(topic_name)["content"]
            self.assertIn("unittest", updated.lower(), second.dependencies.memory_worker.last_error)
            self.assertEqual(len(store.catalog()["entries"]), len(entries), "correction must update the existing topic")
            second.forget_memory(topic_name)
            second.close()

            third = Pico(_build_model_client(SimpleNamespace(model=None)), workspace, sessions.create(root), config=config)
            self.addCleanup(third.close)
            self.assertTrue(third.dependencies.memory_worker.idle.wait(60))
            self.assertNotIn(topic_name, [item["filename"] for item in third.dependencies.memory_store.catalog()["entries"]])
            self.assertNotIn(topic_name, third.memory_index())


if __name__ == "__main__":
    unittest.main()
