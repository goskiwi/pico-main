"""Agent 运行时核心逻辑。

Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import textwrap
import uuid
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import agent_loop
from . import checkpoints
from . import memory as memorylib
from . import security
from . import skills as skillslib
from . import tool_runtime
from .checkpoints import CHECKPOINT_NONE_STATUS
from .config import (
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MAX_DEPTH,
    DEFAULT_FEATURE_FLAGS,
    DEFAULT_MAX_NEW_TOKENS,
    DEFAULT_MAX_STEPS,
    DEFAULT_SHELL_ENV_ALLOWLIST,
)
from .context_manager import ContextManager
from .run_store import RunStore
from .sandbox import DockerSandbox
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
        approval_policy=DEFAULT_APPROVAL_POLICY,
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
        sandbox=None,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.sandbox = sandbox or DockerSandbox(self.root)
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
        self.all_tools = dict(self.tools)
        self.skills = self.load_skills()
        self.last_selected_skills = []
        self.active_tool_names = None
        self.active_tools_strict = False
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
        self.model_action_rejections = []
        self._last_tool_result_metadata = {}
        self._last_sandbox_metadata = {}
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

    def evaluate_resume_state(self):
        return checkpoints.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpoints.render_checkpoint_text(self)

    def build_tools(self):
        return toolkit.build_tool_registry(self)

    def load_skills(self):
        return skillslib.load_skills(self.root)

    def reload_skills(self):
        self.skills = self.load_skills()
        return list(self.skills)

    def select_skills(self, user_message):
        self.last_selected_skills = skillslib.select_skills_with_model(self.model_client, self.skills, user_message)
        self.active_tool_names, self.active_tools_strict = skillslib.compute_active_tools(
            self.last_selected_skills,
            self.all_tools.keys(),
        )
        self.apply_active_tool_filter()
        return list(self.last_selected_skills)

    def apply_active_tool_filter(self):
        previous_signature = self.tool_signature()
        if self.active_tools_strict and self.active_tool_names is not None:
            self.tools = {name: tool for name, tool in self.all_tools.items() if name in self.active_tool_names}
        else:
            self.tools = dict(self.all_tools)
        if self.tool_signature() != previous_signature:
            self._apply_prefix_state(self.build_prefix())

    def identity_metadata(self):
        return {
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "agent_mode": self.agent_mode,
            "depth": self.depth,
            "max_depth": self.max_depth,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "read_only": bool(self.read_only),
            "sandbox": self.sandbox.identity(),
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
        native_actions = bool(getattr(self.model_client, "supports_native_actions", False))
        if native_actions:
            action_rules = """\
            - Call exactly one provided function in every response.
            - Use submit_final only after the requested work and verification are complete.
            - Do not narrate outside a function call.
            - Supply every function argument; use the documented default value when appropriate."""
            examples = "Use the provider's strict function-calling interface."
        else:
            action_rules = """\
            - Return exactly one <tool>...</tool> or one <final>...</final>.
            - Tool calls must look like:
              <tool>{"name":"tool_name","args":{...}}</tool>
            - For write_file and patch_file with multi-line text, prefer XML style:
              <tool name="write_file" path="file.py"><content>...</content></tool>
            - Final answers must look like:
              <final>your answer</final>"""
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
            {action_rules}
            - Never invent tool results.
            - Keep answers concise and concrete.
            - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
            - Before writing tests for existing code, read the implementation first.
            - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
            - Before finalizing an implementation task, verify every explicit behavioral constraint. When constraints interact, add or run at least one discriminating test that exercises the interaction, not only happy paths.
            - New files should be complete and runnable, including obvious imports.
            - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
            - After a patch_file mismatch, correct old_text from the latest file content or use write_file with the complete file; do not keep reading an unchanged file.
            - When a task graph node has a ref, use read_tool_output instead of manually reading tool_outputs paths.
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
        return self.context_manager.history_renderer.history_text()

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    def summarize_tool_result(self, name, args, result):
        """为 history 生成工具输出的简要摘要。

        完整输出已通过 RunStore.save_tool_output 落盘，
        history 里只保留这个摘要和文件引用，保持 prompt 精简。
        """
        result = str(result or "")
        if name == "read_file":
            path = str(args.get("path", ""))
            lines = result.splitlines()
            if len(lines) <= 16:
                preview = "\n".join(lines)
            else:
                omitted = len(lines) - 15
                preview = "\n".join([*lines[:10], f"... ({omitted} lines omitted)", *lines[-5:]])
            return f"read_file {path}: {len(lines)} lines\n{preview}"
        if name == "run_shell":
            command = str(args.get("command", "")).strip()
            lines = [line for line in result.splitlines() if line.strip()]
            key = lines[:4] if lines else ["(empty)"]
            return f"run_shell {command}\n" + "\n".join(key)
        if name in ("write_file", "patch_file"):
            path = str(args.get("path", ""))
            has_error = "error" in result.lower() or "not available" in result.lower()
            status = "(error)" if has_error else "(ok)"
            detail = f" {result[:120]}" if has_error else ""
            return f"{name} {path}: {status}{detail}"
        if name in ("list_files", "search"):
            lines = [line for line in result.splitlines() if line.strip()][:5]
            return f"{name}: " + (" | ".join(lines) if lines else "(empty)")
        if name in ("delegate", "delegate_many"):
            return result[:400] if result else "(empty)"
        return result[:200] if result else "(empty)"

    def prompt_metadata(self, user_message):
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
                "active_tool_names": sorted(self.active_tool_names) if self.active_tool_names is not None else None,
                "active_tools_strict": bool(self.active_tools_strict),
                "skill_count": len(getattr(self, "skills", []) or []),
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

    def create_checkpoint(self, task_state, user_message, trigger):
        return checkpoints.create_checkpoint(self, task_state, user_message, trigger)

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

    def ask(self, user_message):
        return agent_loop.run_agent_turn(self, user_message)

    def run_tool(self, name, args):
        return tool_runtime.run_tool(self, name, args)

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        if self.active_tool_names is not None and name not in self.active_tool_names:
            allowed = ", ".join(sorted(self.active_tool_names)) or "(none)"
            raise ValueError(f"tool '{name}' is not available for the active skills. Available tools: {allowed}")
        toolkit.validate_tool(self, name, args)
        if name in {"delegate", "delegate_many"}:
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")

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
