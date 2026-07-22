"""运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。
"""

import json
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    fcntl = None


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class RunStore:
    # Delegate children share a run root but may receive distinct RunStore
    # instances.  Keep one process-local lock per index path so the index
    # read/modify/write sequence cannot drop a sibling run.
    _index_locks = {}
    _index_locks_guard = threading.Lock()

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_lock = self._lock_for_index(self.index_path())

    @classmethod
    def _lock_for_index(cls, path):
        key = str(Path(path).resolve())
        with cls._index_locks_guard:
            lock = cls._index_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._index_locks[key] = lock
            return lock

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def index_path(self):
        return self.root / "index.json"

    def index_lock_path(self):
        return self.root / "index.json.lock"

    def task_graph_path(self, run_id):
        return self.run_dir(run_id) / "task_graph.mmd"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        self.write_task_graph(task_state, self.initial_task_graph(task_state))
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, task_state.to_dict())
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条落盘比“最后一次性写整份 trace”更稳，也更适合调试。
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return path

    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        self.update_index(task_state)
        return path

    def tool_output_dir(self, run_id):
        path = self.run_dir(run_id) / "tool_outputs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_tool_output(self, task_state, step_index, tool_name, content):
        """将工具完整输出写入磁盘，返回相对 run_dir 的路径。

        history 只存摘要和这个路径引用，完整内容留在磁盘上，
        需要时可以通过 read_tool_output 或审计链路读取。
        返回相对路径（如 tool_outputs/0001_read_file.txt），
        既短又避免把绝对路径写进 task_graph.mmd。
        """
        output_dir = self.tool_output_dir(task_state)
        safe_name = str(tool_name).replace("/", "_").replace("\\", "_")
        filename = f"{step_index:04d}_{safe_name}.txt"
        path = output_dir / filename
        path.write_text(str(content or ""), encoding="utf-8")
        return str(path.relative_to(self.run_dir(task_state)))

    def initial_task_graph(self, task_state):
        return "\n".join(
            [
                "flowchart TD",
                f'  G["goal | open | {self._mmd_label(task_state.user_request)}"]',
            ]
        )

    def append_task_graph_tool(self, task_state, node_id, tool_name, args, status, content_ref):
        path = self.task_graph_path(task_state)
        existing = path.read_text(encoding="utf-8") if path.exists() else self.initial_task_graph(task_state)
        graph_lines = [line.rstrip() for line in existing.splitlines() if line.strip()]
        node_label = self._tool_node_label(tool_name, args, status)
        previous_node = self._last_graph_node_id(graph_lines) or "G"
        graph_lines.append(f'  {node_id}["{node_label}"]')
        graph_lines.append(f"  {previous_node} --> {node_id}")
        # ref 放在独立注释行，不参与 label 截断，也不进入 Mermaid 渲染。
        # 否则长命令或深层绝对路径会把行尾的 ref 截没，read_tool_output 就无法回溯原文。
        graph_lines.append(f"  %% {node_id} ref: {content_ref}")
        self.write_task_graph(task_state, "\n".join(graph_lines))
        return path

    def write_task_graph(self, task_state, content):
        path = self.task_graph_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or "").rstrip() + "\n", encoding="utf-8")
        return path

    def update_index(self, task_state):
        path = self.index_path()
        # Atomic replace prevents readers from observing partial JSON.  The
        # lock additionally protects the read/modify/write transaction itself;
        # replace alone would still allow two children to overwrite each
        # other's newly appended entry.
        with self._index_lock:
            with self._process_index_lock():
                existing = []
                if path.exists():
                    try:
                        loaded = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(loaded, list):
                            existing = loaded
                    except json.JSONDecodeError:
                        existing = []
                entry = {
                    "run_id": task_state.run_id,
                    "task_id": task_state.task_id,
                    "task_goal": str(task_state.user_request),
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "agent_mode": str(getattr(task_state, "agent_mode", "main") or "main"),
                    "parent_agent_id": str(getattr(task_state, "parent_agent_id", "") or ""),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "task_graph_path": str(self.task_graph_path(task_state.run_id)),
                    "report_path": str(self.report_path(task_state.run_id)),
                }
                updated = [item for item in existing if item.get("run_id") != task_state.run_id]
                updated.append(entry)
                self._write_json_atomic(path, updated)
        return path

    @contextmanager
    def _process_index_lock(self):
        """Serialize index transactions across Pico processes on POSIX hosts."""
        if fcntl is None:
            # Non-POSIX platforms retain the process-local thread lock and
            # atomic replacement.  Callers still get valid JSON, but launching
            # multiple writers for one run root is not supported there.
            yield
            return

        lock_path = self.index_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load_recent_index(self, limit=5, *, include_children=False):
        path = self.index_path()
        if not path.exists():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(loaded, list):
            return []
        entries = [item for item in loaded if isinstance(item, dict)]
        if not include_children:
            # Entries produced before agent identity was indexed are main runs
            # by default.  New delegate runs are excluded even if either
            # identity field is accidentally absent.
            entries = [
                item
                for item in entries
                if not str(item.get("parent_agent_id", "") or "").strip()
                and str(item.get("agent_mode", "main") or "main") == "main"
            ]
        entries.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return entries[: max(0, int(limit))]

    def load_report(self, task_id):
        return json.loads(self.report_path(task_id).read_text(encoding="utf-8"))

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。
        # 这样即使中途异常，也不容易留下半截 JSON。
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)

    def _tool_node_label(self, tool_name, args, status):
        args = args if isinstance(args, dict) else {}
        status = str(status or "ok")
        if tool_name in {"read_file", "write_file", "patch_file", "search", "list_files"}:
            target = str(args.get("path", "."))
            summary = f"{tool_name} {target}"
        elif tool_name == "run_shell":
            command = str(args.get("command", "shell")).strip()
            summary = f"run_shell {command}"
        else:
            summary = str(tool_name or "tool")
        return self._mmd_label(f"tool | {status} | {summary}")

    def _mmd_label(self, text):
        text = str(text or "").replace("\n", " ").replace('"', "'").replace("`", "")
        text = text.replace("<", "").replace(">", "")
        return text[:220]

    def _last_graph_node_id(self, lines):
        for line in reversed(lines):
            stripped = line.strip()
            if stripped.startswith("%%"):
                continue
            if "-->" in stripped:
                target = stripped.rsplit("-->", 1)[-1].strip()
                if target:
                    return target.split()[0]
            if "[" in stripped and not stripped.startswith("flowchart "):
                return stripped.split("[", 1)[0].strip()
        return ""
