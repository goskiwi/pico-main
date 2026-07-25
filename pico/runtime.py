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
    CONTEXT_RECOVERY_EVIDENCE_TOKENS,
    CONTEXT_RECOVERY_MAX_FILES,
    CONTEXT_RECOVERY_MAX_TOOL_OUTPUTS,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MAX_DEPTH,
    DEFAULT_FEATURE_FLAGS,
    DEFAULT_MAX_NEW_TOKENS,
    DEFAULT_MAX_STEPS,
    DEFAULT_SHELL_ENV_ALLOWLIST,
)
from .context_manager import ContextManager
from .context_types import _token_clip, count_tokens, tokenizer_details
from .repo_map import RepoMap
from .run_store import RunStore
from .sandbox import DockerSandbox
from .semantic_memory import DisabledSemanticMemoryIndex, SemanticMemoryIndex
from . import tools as toolkit
from .workspace import WorkspaceContext, clip, now


_DEDUPLICATED_READ_ONLY_TOOLS = frozenset(
    {"read_file", "list_files", "search", "query_repo_map"}
)


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
        max_step_extension=None,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        depth=0,
        max_depth=DEFAULT_MAX_DEPTH,
        read_only=False,
        dry_run=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        repo_map_budget_tokens=None,
        agent_mode="main",
        agent_id=None,
        parent_agent_id=None,
        allowed_tools=None,
        sandbox=None,
        semantic_memory_config=None,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self._assert_workspace_root()
        self.sandbox = sandbox or DockerSandbox(self.root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = int(max_steps)
        if self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if max_step_extension is None:
            max_step_extension = self.max_steps
        if isinstance(max_step_extension, bool):
            raise ValueError("max_step_extension must be a non-negative integer")
        try:
            self.max_step_extension = int(max_step_extension)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_step_extension must be a non-negative integer") from exc
        if self.max_step_extension < 0:
            raise ValueError("max_step_extension must be a non-negative integer")
        self.hard_max_steps = self.max_steps + self.max_step_extension
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
        if repo_map_budget_tokens is None:
            self.repo_map_budget_tokens = None
        else:
            if isinstance(repo_map_budget_tokens, bool):
                raise ValueError("repo_map_budget_tokens must be a positive integer")
            try:
                self.repo_map_budget_tokens = int(repo_map_budget_tokens)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "repo_map_budget_tokens must be a positive integer"
                ) from exc
            if self.repo_map_budget_tokens <= 0:
                raise ValueError("repo_map_budget_tokens must be a positive integer")
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".pico" / "runs")
        self.semantic_memory_config = semantic_memory_config
        self.semantic_memory = (
            SemanticMemoryIndex(
                semantic_memory_config,
                workspace_id=hashlib.sha256(
                    str(self.root).encode("utf-8")
                ).hexdigest(),
            )
            if semantic_memory_config is not None
            else DisabledSemanticMemoryIndex()
        )
        self.session = (
            session
            if session is not None
            else {
                "id": datetime.now().strftime("%Y%m%d-%H%M%S")
                + "-"
                + uuid.uuid4().hex[:6],
                "created_at": now(),
                "workspace_root": workspace.repo_root,
                "session_kind": "delegate" if self.parent_agent_id else "main",
                "agent_mode": self.agent_mode,
                "parent_agent_id": self.parent_agent_id,
                "memory": memorylib.default_memory_state(),
                "checkpoints": {"current_id": "", "items": {}},
                "runtime_identity": {},
                "resume_state": {},
            }
        )
        self._ensure_session_shape()
        self.memory = memorylib.LayeredMemory(
            self.session["memory"],
            workspace_root=self.root,
            semantic_index=self.semantic_memory,
        )
        self.session["memory"] = self.memory.to_dict()
        self.repo_map = RepoMap(self.root)
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
        self.current_undo_journal = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self.last_llm_durable_promotions = []
        self.last_llm_durable_rejections = []
        self.last_llm_durable_superseded = []
        self.last_llm_memory_extractor_error = ""
        self.last_semantic_memory_sync = dict(self.memory.last_semantic_sync)
        self.tool_audit_log = []
        self.model_action_rejections = []
        self._last_tool_result_metadata = {}
        self._last_tool_full_result = None
        self._delegate_outcome_metadata = {}
        self._last_sandbox_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }
        self._workspace_snapshot_hash_cache = {}
        self._read_only_tool_signatures = set()
        self._read_only_tool_evidence = {}
        self._context_recovery_text = ""

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
        if self.session.get("session_kind") not in {"main", "delegate"}:
            raise ValueError("session_kind must be 'main' or 'delegate'")
        expected_types = {
            "id": str,
            "agent_mode": str,
            "parent_agent_id": str,
            "memory": dict,
            "checkpoints": dict,
            "runtime_identity": dict,
            "resume_state": dict,
        }
        for name, expected_type in expected_types.items():
            if not isinstance(self.session.get(name), expected_type):
                raise ValueError(f"invalid session field: {name}")
        checkpoints = self.session["checkpoints"]
        if not isinstance(checkpoints.get("current_id"), str):
            raise ValueError("invalid session field: checkpoints.current_id")
        if not isinstance(checkpoints.get("items"), dict):
            raise ValueError("invalid session field: checkpoints.items")

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
            "repo_map_budget_tokens": self.repo_map_budget_tokens,
            "read_only": bool(self.read_only),
            "semantic_memory_enabled": bool(self.semantic_memory.enabled),
            "semantic_memory_status": str(
                self.semantic_memory.last_sync.get("status", "not_run")
            ),
            "workspace_root": str(self.root.resolve()),
            "sandbox": self.sandbox.identity(),
        }

    def _assert_workspace_root(self, workspace=None):
        workspace = workspace or self.workspace
        expected_root = self.root.resolve()
        actual_root = Path(workspace.repo_root).resolve()
        if actual_root != expected_root:
            raise RuntimeError(
                "workspace root invariant violated: "
                f"expected {expected_root}, got {actual_root}"
            )
        return actual_root

    def tool_signature(self):
        payload = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            payload.append(
                {
                    "name": name,
                    "schema": toolkit.strict_response_schema(tool["args_schema"]),
                    "capability": tool.get("capability", ""),
                    "risky": tool["risky"],
                    "description": tool["description"],
                }
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def build_prefix(self):
        tool_lines = []
        for name, tool in self.tools.items():
            fields = ", ".join(
                f"{key}: {value}"
                for key, value in toolkit.schema_display(tool["args_schema"]).items()
            )
            risk = "approval required" if tool["risky"] else "safe"
            capability = tool.get("capability", "read")
            tool_lines.append(f"- {name}({fields}) [{risk}; capability={capability}] {tool['description']}")
        tool_text = "\n".join(tool_lines)
        action_rules = """\
            - Call exactly one provided function in every response.
            - Use submit_final only after the requested work and verification are complete.
            - Do not narrate outside a function call.
            - Supply every function argument; use the documented default value when appropriate."""
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
            - When Workspace provides a verification_command, use it for test verification unless current test output proves it is unsuitable.
            - New files should be complete and runnable, including obvious imports.
            - Never repeat read_file, list_files, search, or query_repo_map with identical arguments while the workspace is unchanged. Reuse the saved evidence, choose a different range or query, run a test, or make the needed edit.
            - After a patch_file mismatch, correct old_text from the latest file content or use write_file with the complete file; do not keep reading an unchanged file.
            - Use read_task_canvas to inspect the active task map (or an archived phase by phase_id), read_task_event for a node summary, and read_tool_output only when the saved evidence is needed.
            - Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, delegate, or delegate_many with args={{}}.

            Tools:
            {tool_text}

            Action protocol:
            Use the provider's strict function-calling interface.

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
        previous_hash = self.prefix_state.hash
        previous_workspace_fingerprint = self.prefix_state.workspace_fingerprint

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        self._assert_workspace_root()
        # The runtime root is a fixed capability boundary. Refresh repository
        # facts inside that boundary instead of rediscovering an enclosing Git
        # repository, which could expose parent files to delegated children.
        refreshed_workspace = WorkspaceContext.build(
            self.root,
            repo_root_override=self.root,
        )
        self._assert_workspace_root(refreshed_workspace)
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

    def count_tokens(self, text):
        """Use the provider model's tokenizer, with a declared tiktoken fallback."""
        counter = getattr(self.model_client, "count_tokens", None)
        if callable(counter):
            return int(counter(text))
        return count_tokens(text, model=str(getattr(self.model_client, "model", "")))

    def tokenizer_metadata(self):
        details = getattr(self.model_client, "tokenizer_metadata", None)
        if callable(details):
            return dict(details())
        return tokenizer_details(str(getattr(self.model_client, "model", "")))

    def task_context_text(self):
        task_state = self.current_task_state
        if task_state is None:
            return "Task state:\n- no active task"
        lines = [
            "Task state (the full Mermaid canvas is for UI, audit, and recovery):",
            f"- goal: {clip(task_state.user_request, 500)}",
            f"- tool steps: {task_state.tool_steps}",
            f"- last tool: {task_state.last_tool or 'none'}",
            f"- canvas: {self.run_store.task_canvas_path(task_state.run_id)}",
            "- In this live task, the conversation's tool results are the authoritative evidence.",
        ]
        if self._context_recovery_text:
            lines.append(self._context_recovery_text)
        return "\n".join(lines)

    def prepare_context_recovery(self):
        """Build a bounded fresh-session bundle after a provider context error.

        This is deliberately reactive: the exact tool-result conversation stays
        intact until the provider says it no longer fits.  The recovery bundle
        then preserves the newest raw observations and fresh versions of the
        files they touched, while the Mermaid canvas remains an audit index.
        """
        task_state = self.current_task_state
        if task_state is None or self.current_run_dir is None:
            self._context_recovery_text = ""
            return {"tool_outputs": 0, "files": 0, "tokens": 0}

        events = []
        offload_path = self.run_store.offload_path(task_state.run_id)
        if offload_path.exists():
            for line in offload_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        recent_events = events[-CONTEXT_RECOVERY_MAX_TOOL_OUTPUTS:]

        def append_with_budget(parts, text, remaining):
            if remaining <= 0:
                return remaining
            rendered = _token_clip(text, remaining, token_counter=self.count_tokens)
            if rendered:
                parts.append(rendered)
                return max(0, remaining - self.count_tokens(rendered))
            return remaining

        parts = [
            "Context recovery after a provider context-limit error:",
            "- This is a fresh model session. The evidence below is authoritative; do not re-read an unchanged file just to reconstruct it.",
            "- Continue the same task, repair or verify from this evidence, and use the task canvas only to navigate archived details.",
        ]
        remaining = max(0, int(CONTEXT_RECOVERY_EVIDENCE_TOKENS) - self.count_tokens("\n".join(parts)))
        # Reserve one third for current file snapshots.  Raw tool output can
        # otherwise fill the whole bundle and recreate the stale-source
        # problem that caused the recovery in the first place.
        evidence_budget = (remaining * 2) // 3
        evidence_remaining = evidence_budget
        output_count = 0
        touched_paths = []
        for event in reversed(recent_events):
            args = dict(event.get("args") or {})
            event_paths = (
                [
                    str(file_args.get("path", "")).strip()
                    for file_args in args.get("files", [])
                    if isinstance(file_args, dict) and str(file_args.get("path", "")).strip()
                ]
                if event.get("tool_name") == "read_file"
                else [str(args.get("path", "")).strip()]
            )
            for path in event_paths:
                if path and path not in touched_paths:
                    touched_paths.append(path)
            ref = str(event.get("result_ref", "")).strip()
            if not ref or Path(ref).is_absolute() or ".." in Path(ref).parts:
                continue
            ref_path = (self.current_run_dir / ref).resolve()
            refs_dir = self.run_store.refs_dir(task_state.run_id).resolve()
            try:
                ref_path.relative_to(refs_dir)
            except ValueError:
                continue
            if not ref_path.is_file():
                continue
            raw_output = ref_path.read_text(encoding="utf-8", errors="replace")
            heading = (
                f"\nLatest tool evidence {event.get('node_id', '')} | "
                f"{event.get('tool_name', '')} | args={json.dumps(args, ensure_ascii=False, sort_keys=True)}:\n"
            )
            before = len(parts)
            evidence_remaining = append_with_budget(parts, heading + raw_output, evidence_remaining)
            output_count += int(len(parts) > before)
            if evidence_remaining <= 0:
                break

        file_count = 0
        file_remaining = remaining - evidence_budget
        for relative_path in reversed(touched_paths):
            if file_count >= CONTEXT_RECOVERY_MAX_FILES or file_remaining <= 0:
                break
            try:
                path = self.path(relative_path)
            except (ValueError, OSError):
                continue
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            heading = f"\nFresh workspace snapshot | {relative_path}:\n"
            before = len(parts)
            file_remaining = append_with_budget(parts, heading + content, file_remaining)
            file_count += int(len(parts) > before)

        self._context_recovery_text = "\n".join(parts)
        return {
            "tool_outputs": output_count,
            "files": file_count,
            "tokens": self.count_tokens(self._context_recovery_text),
        }

    def clear_context_recovery(self):
        self._context_recovery_text = ""

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def summarize_tool_result(self, name, args, result):
        """为 task canvas 与 offload 生成工具输出的简要摘要。

        完整输出保留在 ``refs/*.txt``；这个摘要只服务于可折叠的
        canvas 和可寻址的 offload 事件，不参与 provider 会话。
        """
        result = str(result or "")
        if name == "read_file":
            lines = result.splitlines()
            if len(lines) <= 16:
                preview = "\n".join(lines)
            else:
                omitted = len(lines) - 15
                preview = "\n".join([*lines[:10], f"... ({omitted} lines omitted)", *lines[-5:]])
            paths = [
                str(file_args.get("path", "")).strip()
                for file_args in args.get("files", [])
                if isinstance(file_args, dict) and str(file_args.get("path", "")).strip()
            ]
            return f"read_file {', '.join(paths)}: {len(lines)} lines\n{preview}"
        if name == "run_shell":
            command = str(args.get("command", "")).strip()
            lines = [line for line in result.splitlines() if line.strip()]
            if "pytest" in command.split():
                pytest_summary = [
                    line
                    for line in lines
                    if any(
                        token in line.lower()
                        for token in (
                            "failed",
                            "passed",
                            "error",
                            "skipped",
                            "xfailed",
                            "xpassed",
                        )
                    )
                ]
                key = list(dict.fromkeys([*lines[:2], *pytest_summary[-2:]]))
            else:
                key = lines[:4]
            key = key[:4] if key else ["(empty)"]
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
                "task_context_chars": len(self.task_context_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "active_tool_names": sorted(self.active_tool_names) if self.active_tool_names is not None else None,
                "active_tools_strict": bool(self.active_tools_strict),
                "skill_count": len(self.skills),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(self.model_client.supports_prompt_cache),
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
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进入
        run artifact；这里只挑少量“下一轮大概率还会用到”的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果落盘后，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        if name == "read_file":
            file_args = [
                item for item in args.get("files", [])
                if isinstance(item, dict) and str(item.get("path", "")).strip()
            ]
            sections = self._read_file_result_sections(result)
            for index, item in enumerate(file_args):
                path = str(item["path"])
                canonical_path = self.memory.canonical_path(path)
                self.memory.remember_file(canonical_path)
                summary = memorylib.summarize_read_result(
                    sections[index] if index < len(sections) else result
                )
                self.memory.set_file_summary(canonical_path, summary)
                self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
        elif name in {"write_file", "patch_file"}:
            path = args.get("path")
            if not path:
                return
            canonical_path = self.memory.canonical_path(path)
            self.memory.remember_file(canonical_path)
            self.memory.invalidate_file_summary(canonical_path)

    def _read_file_result_sections(self, result):
        """Extract one batch-read result so each file gets its own memory summary."""
        text = str(result)
        header = re.compile(
            r"^=== read_file metadata: .+; header and line numbers are not file content ===$",
            flags=re.MULTILINE,
        )
        matches = list(header.finditer(text))
        return [
            text[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()
            for index, match in enumerate(matches)
        ]

    def update_working_state(self, **updates):
        if not self.feature_enabled("memory"):
            return
        self.memory.update_working_state(**updates)
        self.session["memory"] = self.memory.to_dict()
        self.session_path = self.session_store.save(self.session)

    def mark_work_started(self, user_message):
        # A task may legitimately inspect the same file as a prior task.  The
        # strict read de-duplication window is intentionally scoped to one ask.
        self._read_only_tool_signatures.clear()
        self._read_only_tool_evidence.clear()
        self.clear_context_recovery()
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

    @staticmethod
    def _read_only_tool_signature(name, args):
        return json.dumps(
            [str(name or ""), args or {}],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def is_duplicate_read_only_tool(self, name, args):
        if name not in _DEDUPLICATED_READ_ONLY_TOOLS:
            return False
        return self._read_only_tool_signature(name, args) in self._read_only_tool_signatures

    def cached_read_only_evidence(self, name, args):
        signature = self._read_only_tool_signature(name, args)
        return dict(self._read_only_tool_evidence.get(signature, {}))

    def cache_read_only_evidence(self, name, args, result, *, result_ref="", node_id=""):
        if name not in _DEDUPLICATED_READ_ONLY_TOOLS:
            return
        signature = self._read_only_tool_signature(name, args)
        self._read_only_tool_signatures.add(signature)
        # Preserve the first observation for this workspace version.  A later
        # duplicate must never replace the original source evidence with its
        # own rejection message.
        self._read_only_tool_evidence.setdefault(
            signature,
            {
                "result": str(result or ""),
                "result_ref": str(result_ref or ""),
                "node_id": str(node_id or ""),
            },
        )

    def mark_tool_finished(self, name, args, metadata, result):
        # A successful workspace change invalidates all prior read evidence.
        # The next read may therefore reuse the same tool arguments.
        if metadata.get("workspace_changed"):
            self._read_only_tool_signatures.clear()
            self._read_only_tool_evidence.clear()
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
            current_subtask="recovering from a rejected model action",
            next_action="ask the model for one valid function call",
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
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(
            self.session["memory"],
            workspace_root=self.root,
            semantic_index=self.semantic_memory,
        )
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
