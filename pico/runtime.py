"""Agent 运行时核心逻辑。

Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import re
import textwrap
import uuid
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import checkpoints
from . import durable_memory
from . import memory as memorylib
from . import security
from . import tool_runtime
from .checkpoints import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .config import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_FEATURE_FLAGS,
    MAX_HISTORY,
    DEFAULT_MAX_NEW_TOKENS,
    DEFAULT_MAX_STEPS,
    DEFAULT_SHELL_ENV_ALLOWLIST,
    MEMORY_EXTRACTOR_MAX_TOKENS,
)
from .context_manager import ContextManager
from .parser import parse_model_output
from .run_store import RunStore
from .task_state import TaskState
from . import tools as toolkit
from .workspace import WorkspaceContext, clip, now


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


def new_agent_id():
    return "agent_" + uuid.uuid4().hex[:8]


class Pico:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=DEFAULT_MAX_STEPS,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        depth=0,
        max_depth=DEFAULT_MAX_DEPTH,
        read_only=False,
        dry_run=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        agent_mode="main",
        agent_id=None,
        parent_agent_id=None,
        allowed_tools=None,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.dry_run = bool(dry_run)
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.agent_mode = str(agent_mode or "main")
        self.agent_id = str(agent_id or new_agent_id())
        self.parent_agent_id = str(parent_agent_id or "")
        self.allowed_tools = None if allowed_tools is None else tuple(str(name) for name in allowed_tools)
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".pico" / "runs")
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self._ensure_session_shape()
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()
        self.tools = self.build_tools()
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self.last_llm_durable_promotions = []
        self.last_llm_durable_rejections = []
        self.last_llm_durable_superseded = []
        self.last_llm_memory_extractor_error = ""
        self.tool_audit_log = []
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def _ensure_session_shape(self):
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

    def current_runtime_identity(self):
        return checkpoints.current_runtime_identity(self)

    def checkpoint_state(self):
        return checkpoints.checkpoint_state(self)

    def current_checkpoint(self):
        return checkpoints.current_checkpoint(self)

    def invalidate_stale_memory(self):
        return checkpoints.invalidate_stale_memory(self)

    def evaluate_resume_state(self):
        return checkpoints.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpoints.render_checkpoint_text(self)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        return toolkit.build_tool_registry(self)

    def identity_metadata(self):
        return {
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "agent_mode": self.agent_mode,
            "depth": self.depth,
            "max_depth": self.max_depth,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "read_only": bool(self.read_only),
        }

    def tool_signature(self):
        payload = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            payload.append(
                {
                    "name": name,
                    "schema": tool["schema"],
                    "capability": tool.get("capability", ""),
                    "risky": tool["risky"],
                    "description": tool["description"],
                }
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def build_prefix(self):
        tool_lines = []
        for name, tool in self.tools.items():
            fields = ", ".join(f"{key}: {value}" for key, value in toolkit.schema_display(tool["schema"]).items())
            risk = "approval required" if tool["risky"] else "safe"
            capability = tool.get("capability", "read")
            tool_lines.append(f"- {name}({fields}) [{risk}; capability={capability}] {tool['description']}")
        tool_text = "\n".join(tool_lines)
        examples = "\n".join(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
                '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
                '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
                '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
                "<final>Done.</final>",
            ]
        )
        # prefix 可以理解成 agent 的“工作手册”：
        # 它是谁、工具怎么调用、当前仓库是什么状态，都写在这里。
        text = textwrap.dedent(
            f"""\
            You are pico, a small local coding agent working inside a local repository.

            Agent identity:
            - id: {self.agent_id}
            - mode: {self.agent_mode}
            - parent id: {self.parent_agent_id or "none"}
            - depth: {self.depth}/{self.max_depth}
            - read only: {bool(self.read_only)}

            Rules:
            - Use tools instead of guessing about the workspace.
            - Return exactly one <tool>...</tool> or one <final>...</final>.
            - Tool calls must look like:
              <tool>{{"name":"tool_name","args":{{...}}}}</tool>
            - For write_file and patch_file with multi-line text, prefer XML style:
              <tool name="write_file" path="file.py"><content>...</content></tool>
            - Final answers must look like:
              <final>your answer</final>
            - Never invent tool results.
            - Keep answers concise and concrete.
            - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
            - Before writing tests for existing code, read the implementation first.
            - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
            - New files should be complete and runnable, including obvious imports.
            - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
            - Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, delegate, or delegate_many with args={{}}.

            Tools:
            {tool_text}

            Valid response examples:
            {examples}

            {self.workspace.text()}
            """
        ).strip()
        return PromptPrefix(
            text=text,
            hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            workspace_fingerprint=self.workspace.fingerprint(),
            tool_signature=self.tool_signature(),
            built_at=now(),
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return self.memory.render_memory_text()

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
                **self.identity_metadata(),
            }
        )
        metadata.update(security.detected_secret_env_summary(self))
        return prompt, metadata

    def emit_trace(self, task_state, event, payload=None):
        payload = security.redact_artifact(self, payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答“这一轮 agent 到底做了什么”。
        self.run_store.append_trace(task_state, payload)
        return payload

    def capture_workspace_snapshot(self):
        return tool_runtime.capture_workspace_snapshot(self)

    @staticmethod
    def diff_workspace_snapshots(before, after):
        return tool_runtime.diff_workspace_snapshots(before, after)

    def create_checkpoint(self, task_state, user_message, trigger):
        return checkpoints.create_checkpoint(self, task_state, user_message, trigger)

    def infer_next_step(self, task_state):
        return checkpoints.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到 working memory。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量“下一轮大概率还会用到”的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)
        if name == "read_file":
            summary = memorylib.summarize_read_result(result)
            self.memory.set_file_summary(canonical_path, summary)
            self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
        elif name in {"write_file", "patch_file"}:
            self.memory.invalidate_file_summary(canonical_path)

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def update_working_state(self, **updates):
        if not self.feature_enabled("memory"):
            return
        self.memory.update_working_state(**updates)
        self.session["memory"] = self.memory.to_dict()
        self.session_path = self.session_store.save(self.session)

    def mark_work_started(self, user_message):
        self.update_working_state(
            goal=user_message,
            current_subtask="building prompt and asking model",
            next_action="parse the model response",
            last_error="",
        )

    def mark_tool_planned(self, name):
        self.update_working_state(
            current_subtask=f"running tool {name}",
            next_action="inspect the tool result",
        )

    def mark_tool_finished(self, name, metadata, result):
        status = str(metadata.get("tool_status", "")).strip() or "ok"
        error_code = str(metadata.get("tool_error_code", "")).strip()
        if status in {"error", "rejected", "partial_success"}:
            self.update_working_state(
                current_subtask=f"handled tool {name} with status {status}",
                next_action="recover from the tool result",
                last_error=clip(f"{name} {status}: {error_code or result}", 240),
            )
            return
        self.update_working_state(
            current_subtask=f"processed tool {name}",
            next_action="continue reasoning from the tool result",
            last_error="",
        )

    def mark_retry_needed(self, notice):
        self.update_working_state(
            current_subtask="recovering from malformed model output",
            next_action="ask the model for a valid tool call or final answer",
            last_error=clip(notice, 240),
        )

    def mark_work_finished(self, final, stopped=False):
        self.update_working_state(
            current_subtask="stopped" if stopped else "completed",
            next_action="-",
            last_error=clip(final, 240) if stopped else "",
        )

    def record_process_note_for_tool(self, name, metadata):
        return tool_runtime.record_process_note_for_tool(self, name, metadata)

    def reject_durable_reason(self, note_text):
        return durable_memory.reject_reason(note_text)

    def extract_durable_promotions(self, user_message, final_answer):
        return durable_memory.extract_promotions(user_message, final_answer)

    def promote_durable_memory(self, user_message, final_answer):
        result = durable_memory.promote(self.memory, user_message, final_answer)
        self.session["memory"] = self.memory.to_dict()
        self.last_durable_promotions = result.promoted
        self.last_durable_rejections = result.rejections
        self.last_durable_superseded = result.superseded
        return result.promoted, result.rejections, result.superseded

    def llm_memory_index_text(self):
        if not getattr(self.memory, "durable_store", None):
            return "- none"
        entries = self.memory.durable_store.load_index()
        if not entries:
            return "- none"
        lines = []
        for entry in entries:
            memory_type = entry.get("type", "")
            summary = entry.get("summary", "")
            notes = self.memory.durable_store.load_type_notes(memory_type)
            lines.append(f"- {memory_type}: {summary}")
            for note in notes[:8]:
                description = str(note.get("description") or note.get("text") or "").strip()
                if description:
                    lines.append(f"  - {clip(description, 160)}")
        return "\n".join(lines) if lines else "- none"

    def build_memory_extractor_prompt(self, user_message, final_answer):
        return textwrap.dedent(
            f"""\
            You are pico's durable memory extractor. Decide whether the latest completed turn contains long-term memory worth saving.

            Allowed memory types:
            - user: stable facts about the user, their background, or skill profile.
            - feedback: user preferences, corrections, and behavioral guidance for future agent work.
            - project: stable project constraints, decisions, policies, or durable project dynamics.
            - reference: durable pointers to external sources of truth or where to look things up.

            Save only information that is likely to remain useful across future sessions.
            Do not save code facts, file paths, line numbers, git history, current task state, tool output, stack traces, secrets, or temporary debugging details.
            Do not infer user traits from a single question. Prefer explicit user feedback, corrections, or stated constraints.
            If a candidate is a feedback or project rule, include the rule as a concise standalone sentence in text. Include why/how only when the user supplied that rationale.

            Existing durable memory index:
            {self.llm_memory_index_text()}

            Latest user message:
            {user_message}

            Final assistant answer:
            {final_answer}

            Return JSON only, with this shape:
            {{"memories":[{{"type":"user|feedback|project|reference","text":"concise memory text"}}]}}
            Return {{"memories":[]}} if there is nothing worth saving.
            """
        ).strip()

    @staticmethod
    def parse_memory_extractor_output(raw):
        text = str(raw or "").strip()
        if not text:
            return []
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        if not text.startswith("{"):
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise ValueError("extractor returned no JSON object")
            text = match.group(0)
        data = json.loads(text)
        memories = data.get("memories", [])
        if not isinstance(memories, list):
            raise ValueError("extractor memories must be a list")
        candidates = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            memory_type = str(item.get("type", "")).strip()
            note_text = str(item.get("text", "")).strip()
            if memory_type and note_text:
                candidates.append((memory_type, note_text))
        return candidates

    def llm_promote_durable_memory(self, user_message, final_answer):
        self.last_llm_durable_promotions = []
        self.last_llm_durable_rejections = []
        self.last_llm_durable_superseded = []
        self.last_llm_memory_extractor_error = ""
        if not self.feature_enabled("llm_memory_extract"):
            return [], [], []
        prompt = self.build_memory_extractor_prompt(user_message, final_answer)
        try:
            raw = self.model_client.complete(prompt, MEMORY_EXTRACTOR_MAX_TOKENS)
            candidates = self.parse_memory_extractor_output(raw)
        except Exception as exc:
            self.last_llm_memory_extractor_error = str(exc)
            self.session["memory"] = self.memory.to_dict()
            return [], [f"extractor_error:{type(exc).__name__}"], []
        result = durable_memory.promote_candidates(self.memory, candidates)
        self.session["memory"] = self.memory.to_dict()
        self.last_llm_durable_promotions = result.promoted
        self.last_llm_durable_rejections = result.rejections
        self.last_llm_durable_superseded = result.superseded
        return result.promoted, result.rejections, result.superseded

    def ask(self, user_message):
        """执行一次完整的 agent 回合，直到产出最终答案或命中停止条件。

        为什么存在：
        `ask()` 是整个 runtime 的总调度器。它把“用户提一个请求”扩展成一条
        可持续推进的控制循环：记录会话、组 prompt、调用模型、执行工具、
        写 trace/report、更新状态，直到模型给出最终答案或系统主动停下。

        输入 / 输出：
        - 输入：`user_message`，即用户这一次的任务描述
        - 输出：字符串形式的最终回答；如果中途达到步数上限或重试上限，
          返回的是一条停止原因说明

        在 agent 链路里的位置：
        它是 CLI 和底层工具/模型之间的核心桥梁。CLI 收到用户输入后基本只做
        一件事：调用 `agent.ask()`。而 `ask()` 内部再去驱动 `ContextManager`
        组 prompt、`model_client.complete()` 调模型、`run_tool()` 执行动作。
        如果新人想理解 pico 是怎么“从一句话跑成一个 agent 流程”的，
        这里就是最关键的入口。
        """
        run_started_at = time.monotonic()
        self.mark_work_started(user_message)
        self.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(run_id=self.new_run_id(), task_id=self.new_task_id(), user_request=user_message)
        task_state.resume_status = self.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        self.current_task_state = task_state
        self.current_run_dir = self.run_store.start_run(task_state)
        self.tool_audit_log = []
        self.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
                **self.identity_metadata(),
            },
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < self.max_steps and attempts < max_attempts:
            attempts += 1
            task_state.record_attempt()
            self.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = self._build_prompt_and_metadata(user_message)
            self.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                self.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="context_reduction")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            self.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(self.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            model_started_at = time.monotonic()
            try:
                raw = self.model_client.complete(
                    prompt,
                    self.max_new_tokens,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                )
            except Exception as exc:
                final = f"Stopped after model error: {type(exc).__name__}: {exc}"
                self.mark_work_finished(final, stopped=True)
                self.record({"role": "assistant", "content": final, "created_at": now()})
                task_state.stop_model_error(final)
                self.last_prompt_metadata = prompt_metadata
                self.last_completion_metadata = {}
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="model_error")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "model_error",
                    },
                )
                self.emit_trace(
                    task_state,
                    "model_failed",
                    {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                    },
                )
                self.emit_trace(
                    task_state,
                    "run_finished",
                    {
                        "status": task_state.status,
                        "stop_reason": task_state.stop_reason,
                        "final_answer": final,
                        "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                    },
                )
                self.run_store.write_report(task_state, security.redact_artifact(self, self.build_report(task_state)))
                return final
            completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            self.last_completion_metadata = completion_metadata
            self.last_prompt_metadata = prompt_metadata
            kind, payload = parse_model_output(raw)
            self.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                self.mark_tool_planned(name)
                task_state.record_tool(name)
                tool_started_at = time.monotonic()
                result = self.run_tool(name, args)
                tool_duration_ms = int((time.monotonic() - tool_started_at) * 1000)
                self.record_tool_audit(name, args, result, tool_duration_ms)
                self.mark_tool_finished(name, dict(self._last_tool_result_metadata or {}), result)
                self.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
                )
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": tool_duration_ms,
                        **dict(self._last_tool_result_metadata or {}),
                    },
                )
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="tool_executed")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
                continue

            if kind == "retry":
                self.mark_retry_needed(payload)
                self.record({"role": "assistant", "content": payload, "created_at": now()})
                self.run_store.write_task_state(task_state)
                continue

            final = (payload or raw).strip()
            self.mark_work_finished(final)
            self.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            self.promote_durable_memory(user_message, final)
            self.llm_promote_durable_memory(user_message, final)
            checkpoint = self.create_checkpoint(task_state, user_message, trigger="run_finished")
            self.run_store.write_task_state(task_state)
            self.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
                },
            )
            self.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            self.run_store.write_report(task_state, security.redact_artifact(self, self.build_report(task_state)))
            return final

        if attempts >= max_attempts and tool_steps < self.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        self.mark_work_finished(final, stopped=True)
        self.record({"role": "assistant", "content": final, "created_at": now()})
        self.promote_durable_memory(user_message, final)
        self.run_store.write_task_state(task_state)
        checkpoint = self.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        self.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        self.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        self.run_store.write_report(task_state, security.redact_artifact(self, self.build_report(task_state)))
        return final

    def run_tool(self, name, args):
        return tool_runtime.run_tool(self, name, args)

    @staticmethod
    def tool_capability(tool):
        return tool_runtime.tool_capability(tool)

    def tool_risk_level(self, tool):
        return tool_runtime.tool_risk_level(tool)

    def tool_permission_error(self, tool):
        return tool_runtime.tool_permission_error(self, tool)

    def dry_run_tool_result(self, name, args):
        return tool_runtime.dry_run_tool_result(name, args)

    @staticmethod
    def shell_policy_metadata(policy):
        return tool_runtime.shell_policy_metadata(policy)

    @staticmethod
    def shell_command_policy(name, args):
        return tool_runtime.shell_command_policy(name, args)

    def repeated_tool_call(self, name, args):
        return tool_runtime.repeated_tool_call(self, name, args)

    def build_report(self, task_state):
        return tool_runtime.build_report(self, task_state)

    def record_tool_audit(self, name, args, result, duration_ms):
        return tool_runtime.record_tool_audit(self, name, args, result, duration_ms)

    def build_run_summary(self, task_state):
        return tool_runtime.build_run_summary(self, task_state)

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self, name, args)
        if name in {"delegate", "delegate_many"}:
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self, args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self, args)

    def tool_search(self, args):
        return toolkit.tool_search(self, args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self, args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self, args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self, args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self, args)

    def tool_delegate_many(self, args):
        return toolkit.tool_delegate_many(self, args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def reset(self):
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root=self.root)
        self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved


MiniAgent = Pico
