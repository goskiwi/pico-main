"""Four-level task-context artifact store.

Each run persists evidence in four progressively lighter forms:

``refs/*.txt`` -> ``offload.jsonl`` -> ``task.mmd`` -> ``index.json``.

The first is the source evidence, the JSONL file makes each tool result
addressable, the Mermaid canvas is the active task view, and the index is the
cross-run entry point.  Session history remains an audit trail; it is not a
prompt-context fallback.
"""

import json
import re
import tempfile
import threading
from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
from pathlib import Path

class RunStore:
    _TASK_NODE_RE = re.compile(r'^\s*(?P<node_id>N\d+_[A-Za-z0-9_]+)\["(?P<label>.*)"\]$')

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
        return self.root / run_id

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

    def task_canvas_path(self, run_id):
        return self.run_dir(run_id) / "task.mmd"

    def offload_path(self, run_id):
        return self.run_dir(run_id) / "offload.jsonl"

    def refs_dir(self, run_id):
        path = self.run_dir(run_id) / "refs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def phases_dir(self, run_id):
        path = self.run_dir(run_id) / "phases"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def phase_index_path(self, run_id):
        return self.phases_dir(run_id) / "index.json"

    def task_phase_path(self, run_id, phase_id):
        phase_id = str(phase_id or "").strip()
        if not re.fullmatch(r"phase_\d{3,}", phase_id):
            raise ValueError("invalid phase_id")
        return self.phases_dir(run_id) / f"{phase_id}.mmd"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        self.write_task_canvas(task_state, self.initial_task_canvas(task_state))
        self.offload_path(task_state.run_id).touch()
        self._write_json_atomic(self.phase_index_path(task_state.run_id), [])
        self.update_index(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, task_state.to_dict())
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条落盘比“最后一次性写整份 trace”更稳，也更适合调试。
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return path

    def write_report(self, task_state, report):
        path = self.report_path(task_state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        self.finalize_task_canvas(task_state)
        self.update_index(task_state)
        return path

    def save_reference(self, task_state, step_index, tool_name, content):
        """Save Level 0 evidence and return a run-relative ``refs`` path."""
        output_dir = self.refs_dir(task_state.run_id)
        safe_name = str(tool_name).replace("/", "_").replace("\\", "_")
        filename = f"{step_index:04d}_{safe_name}.txt"
        path = output_dir / filename
        path.write_text(str(content or ""), encoding="utf-8")
        return str(path.relative_to(self.run_dir(task_state.run_id)))

    def initial_task_canvas(self, task_state):
        return "\n".join(
            [
                "flowchart TD",
                f'  G["goal | running | {self._mmd_label(task_state.user_request)}"]',
            ]
        )

    def append_offload_event(
        self,
        task_state,
        *,
        node_id,
        tool_name,
        args,
        summary,
        status,
        result_ref,
    ):
        """Append a Level 1, node-addressable tool summary."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "node_id": str(node_id),
            "tool_name": str(tool_name),
            "args": dict(args or {}),
            "summary": str(summary),
            "status": str(status),
            "result_ref": str(result_ref),
        }
        path = self.offload_path(task_state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        return record

    def append_task_node(self, task_state, *, node_id, summary, status, result_ref):
        """Attach one completed task step to the active Mermaid canvas.

        Nodes are attached directly to the goal instead of inventing sequential
        dependencies between unrelated tool calls.  The JSONL event is the
        detailed evidence; the canvas is deliberately the lighter navigation
        layer.
        """
        path = self.task_canvas_path(task_state.run_id)
        existing = path.read_text(encoding="utf-8") if path.exists() else self.initial_task_canvas(task_state)
        graph_lines = [line.rstrip() for line in existing.splitlines() if line.strip()]
        node_label = self._mmd_label(f"task_step | {status} | {summary}")
        graph_lines.append(f'  {node_id}["{node_label}"]')
        graph_lines.append(f"  G --> {node_id}")
        graph_lines.append(f"  %% {node_id} ref: {result_ref}")
        self.write_task_canvas(task_state, "\n".join(graph_lines))
        return path

    def fold_task_canvas(
        self,
        task_state,
        *,
        token_counter,
        max_active_nodes,
        retain_nodes,
        max_tokens,
    ):
        """Archive older task-step nodes into a drill-down phase canvas.

        The active ``task.mmd`` remains the small working view used in every
        prompt.  Archived phases retain the exact task nodes and evidence refs
        so a model or report reader can descend only when that detail matters.
        """
        path = self.task_canvas_path(task_state.run_id)
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else self.initial_task_canvas(task_state)
        blocks = self._task_node_blocks(existing)
        active_count = len(blocks)
        current_tokens = int(token_counter(existing))
        max_active_nodes = max(1, int(max_active_nodes))
        retain_nodes = min(max_active_nodes - 1, max(1, int(retain_nodes)))
        max_tokens = max(1, int(max_tokens))
        needs_fold = active_count > max_active_nodes or current_tokens > max_tokens
        if not needs_fold or active_count <= retain_nodes:
            return {
                "folded": False,
                "active_node_count": active_count,
                "task_canvas_tokens": current_tokens,
                "phase_count": len(self._load_phase_index(task_state.run_id)),
            }

        archive_count = max(1, active_count - retain_nodes)
        archived_blocks = blocks[:archive_count]
        active_blocks = blocks[archive_count:]
        phases = self._load_phase_index(task_state.run_id)
        phase_id = f"phase_{len(phases) + 1:03d}"
        phase_status = (
            "attention"
            if any("task_step | blocked |" in block["label"] for block in archived_blocks)
            else "done"
        )
        phase_path = self.task_phase_path(task_state.run_id, phase_id)
        phase_path.write_text(
            self._render_phase_canvas(phase_id, phase_status, archived_blocks),
            encoding="utf-8",
        )
        phase_record = {
            "phase_id": phase_id,
            "path": str(phase_path.relative_to(self.run_dir(task_state.run_id))),
            "status": phase_status,
            "node_ids": [block["node_id"] for block in archived_blocks],
            "node_count": len(archived_blocks),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        phases.append(phase_record)
        self._write_json_atomic(self.phase_index_path(task_state.run_id), phases)
        self.write_task_canvas(
            task_state,
            self._render_active_canvas(existing, active_blocks, phases),
        )
        folded_text = self.task_canvas_path(task_state.run_id).read_text(encoding="utf-8")
        return {
            "folded": True,
            "phase_id": phase_id,
            "archived_node_ids": list(phase_record["node_ids"]),
            "active_node_count": len(active_blocks),
            "task_canvas_tokens": int(token_counter(folded_text)),
            "phase_count": len(phases),
        }

    def write_task_canvas(self, task_state, content):
        path = self.task_canvas_path(task_state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or "").rstrip() + "\n", encoding="utf-8")
        return path

    def finalize_task_canvas(self, task_state):
        path = self.task_canvas_path(task_state.run_id)
        if not path.exists():
            return path
        status = "done" if task_state.status == "completed" else "blocked"
        label = self._mmd_label(task_state.user_request)
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(
            r'^\s*G\[".*"\]$',
            f'  G["goal | {status} | {label}"]',
            text,
            flags=re.MULTILINE,
        )
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return path

    def update_index(self, task_state, *, latest_node_id=""):
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
                previous = next(
                    (item for item in existing if item.get("run_id") == task_state.run_id),
                    {},
                )
                phase_summary = self.phase_summary(task_state.run_id)
                entry = {
                    "run_id": task_state.run_id,
                    "task_id": task_state.task_id,
                    "task_goal": str(task_state.user_request),
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "agent_mode": str(task_state.agent_mode),
                    "parent_agent_id": str(task_state.parent_agent_id),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "latest_node_id": str(latest_node_id or previous.get("latest_node_id", "")),
                    "task_canvas_path": str(self.task_canvas_path(task_state.run_id)),
                    "offload_path": str(self.offload_path(task_state.run_id)),
                    "phase_index_path": str(self.phase_index_path(task_state.run_id)),
                    "phase_count": phase_summary["phase_count"],
                    "archived_node_count": phase_summary["archived_node_count"],
                    "report_path": str(self.report_path(task_state.run_id)),
                }
                updated = [item for item in existing if item.get("run_id") != task_state.run_id]
                updated.append(entry)
                self._write_json_atomic(path, updated)
        return path

    @contextmanager
    def _process_index_lock(self):
        """Serialize index transactions across Pico processes on POSIX hosts."""
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
            entries = [
                item
                for item in entries
                if item.get("parent_agent_id") == ""
                and item.get("agent_mode") == "main"
            ]
        entries.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return entries[: max(0, int(limit))]

    def task_canvas_text(self, run_id, *, phase_id=""):
        phase_id = str(phase_id or "").strip()
        path = self.task_phase_path(run_id, phase_id) if phase_id else self.task_canvas_path(run_id)
        if not path.exists():
            if phase_id:
                raise ValueError(f"task phase not found: {phase_id}")
            return "Task canvas:\n- no active task"
        heading = f"Task phase {phase_id}:" if phase_id else "Task canvas:"
        return heading + "\n" + path.read_text(encoding="utf-8", errors="replace").strip()

    def phase_summary(self, run_id):
        phases = self._load_phase_index(run_id)
        return {
            "phase_count": len(phases),
            "archived_node_count": sum(int(item.get("node_count", 0)) for item in phases),
        }

    def load_task_phases(self, run_id):
        return self._load_phase_index(run_id)

    def read_offload_event(self, run_id, node_id):
        path = self.offload_path(run_id)
        if not path.is_file():
            raise ValueError(f"offload index not found for run_id: {run_id}")
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("node_id") == node_id:
                return record
        raise ValueError(f"node not found in offload index: {node_id}")

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

    def _mmd_label(self, text):
        text = str(text or "").replace("\n", " ").replace('"', "'").replace("`", "")
        text = text.replace("<", "").replace(">", "")
        return text[:220]

    def _load_phase_index(self, run_id):
        path = self.phase_index_path(run_id)
        if not path.exists():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []

    def _task_node_blocks(self, text):
        lines = str(text or "").splitlines()
        blocks = []
        index = 0
        while index < len(lines):
            match = self._TASK_NODE_RE.match(lines[index])
            if match is None:
                index += 1
                continue
            node_id = match.group("node_id")
            block_lines = [lines[index].rstrip()]
            index += 1
            if index < len(lines) and lines[index].strip() == f"G --> {node_id}":
                block_lines.append(lines[index].rstrip())
                index += 1
            if index < len(lines) and lines[index].strip().startswith(f"%% {node_id} ref:"):
                block_lines.append(lines[index].rstrip())
                index += 1
            blocks.append(
                {
                    "node_id": node_id,
                    "label": match.group("label"),
                    "lines": block_lines,
                }
            )
        return blocks

    def _render_active_canvas(self, existing, active_blocks, phases):
        root_line = next(
            (
                line.rstrip()
                for line in str(existing or "").splitlines()
                if line.strip().startswith('G["')
            ),
            '  G["goal | running | task"]',
        )
        lines = ["flowchart TD", root_line]
        if phases:
            phase_count = len(phases)
            node_count = sum(int(phase.get("node_count", 0)) for phase in phases)
            archive_status = "attention" if any(phase.get("status") == "attention" for phase in phases) else "done"
            lines.extend(
                [
                    f'  A["archive | {archive_status} | {phase_count} phases / {node_count} task steps | ref: phases/index.json"]',
                    "  G --> A",
                    "  %% A ref: phases/index.json",
                ]
            )
        for block in active_blocks:
            lines.extend(block["lines"])
        return "\n".join(lines)

    def _render_phase_canvas(self, phase_id, status, blocks):
        lines = [
            "flowchart TD",
            f'  P["{phase_id} | {status} | {len(blocks)} archived task steps"]',
        ]
        for block in blocks:
            node_id = block["node_id"]
            lines.append(block["lines"][0])
            lines.append(f"  P --> {node_id}")
            lines.extend(block["lines"][2:])
        return "\n".join(lines) + "\n"
