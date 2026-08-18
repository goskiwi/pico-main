"""Agent 运行时核心逻辑。

Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 Runtime event、更新工作记忆，以及在合适的时候停下来。
"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import checkpoint as checkpointlib
from . import security as securitylib
from . import tools as toolkit
from .artifacts import ArtifactStore
from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_SCHEMA_VERSION
from .context_manager import ContextManager
from .contracts import ToolCall
from .evidence import EvidenceLedger
from .features import memory as memorylib
from .hooks import HookRunner
from .mutations import WorkspaceMutationService
from .project_memory import MEMORY_SELECTOR_MAX_SELECTED, ProjectMemoryStore
from .prompt_prefix import build_prompt_prefix, tool_signature
from .repo_map import RepoMap
from .repository_overview import discover_repository_overview
from .run_store import RunStore
from .sandbox import DockerSandbox, DockerSandboxConfig
from .session_store import SESSION_SCHEMA_VERSION, SessionStore
from .tool_context import ToolContext
from .tool_executor import ToolExecutor
from .verification import discover_verification_command, run_verification
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, WorkspaceContext, clip, now

DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}
__all__ = ["Pico", "SessionStore"]


class Pico:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=None,
        max_new_tokens=512,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        allowed_tools=None,
        run_timeout_seconds=600,
        provider_context_limit_tokens=64000,
        sandbox=None,
        sandbox_image="pico/sandbox:latest",
        verification_command=None,
        hooks=None,
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.invocation_cwd = Path(workspace.cwd)
        self._workspace_snapshot_cache = None
        self._workspace_content_fingerprint_cache = None
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = None if max_steps is None else max(1, int(max_steps))
        self.max_new_tokens = max_new_tokens
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        self.run_timeout_seconds = max(1, int(run_timeout_seconds))
        self.provider_context_limit_tokens = max(
            int(max_new_tokens) + 1,
            int(provider_context_limit_tokens),
        )
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".pico" / "runs")
        self.artifact_store = ArtifactStore(self.run_store, self.redact_text)
        self.session = session or {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self._ensure_session_shape()
        self.memory = memorylib.SessionWorkingMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()
        self.project_memory = ProjectMemoryStore(self.root / ".pico" / "memory", self.root)
        self.mutation_service = WorkspaceMutationService(self.root)
        self.sandbox = sandbox or DockerSandbox(
            self.root, DockerSandboxConfig(image=str(sandbox_image))
        )
        self.verification_command = (
            discover_verification_command(self.root)
            if verification_command is None else str(verification_command)
        )
        self.hooks = HookRunner(hooks)
        self.evidence_ledger = EvidenceLedger()
        self.repo_map = RepoMap(self.root)
        self.repository_overview = discover_repository_overview(self.root)
        self.all_tools = self.build_tools()
        self.tools = self._apply_tool_allowlist(self.all_tools)
        self.action_tools = toolkit.build_action_tools(self.tools)
        self.tool_executor = ToolExecutor(self)
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_execution = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self._task_memory_selection = None
        self.last_tool_outcome = None
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

    def _ensure_session_shape(self):
        if self.session.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise ValueError("unsupported session schema")
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("schema_version", CHECKPOINT_SCHEMA_VERSION)
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

    def current_runtime_identity(self):
        return checkpointlib.current_runtime_identity(self)

    def checkpoint_state(self):
        return checkpointlib.checkpoint_state(self)

    def current_checkpoint(self):
        return checkpointlib.current_checkpoint(self)

    def invalidate_stale_memory(self):
        invalidated = self.memory.invalidate_stale_file_observations()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def evaluate_resume_state(self):
        return checkpointlib.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpointlib.render_checkpoint_text(self)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        return toolkit.build_tool_registry(self.tool_context())

    @staticmethod
    def _normalize_allowed_tools(allowed_tools):
        if allowed_tools is None:
            return None
        normalized = tuple(str(name).strip() for name in allowed_tools)
        if not normalized or any(not name for name in normalized):
            raise ValueError("allowed_tools must be a non-empty sequence of tool names")
        return normalized

    def _apply_tool_allowlist(self, tools):
        if self.allowed_tools is None:
            return tools
        legal_names = toolkit.legal_tool_names()
        unknown = [name for name in self.allowed_tools if name not in legal_names]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        allowed = set(self.allowed_tools)
        return {
            name: tool
            for name, tool in tools.items()
            if name in allowed
        }

    def tool_signature(self):
        return tool_signature(self.tools)

    def build_prefix(self):
        return build_prompt_prefix(
            workspace=self.workspace,
            tools=self.tools,
            repository_overview=self.repository_overview,
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(
            self.invocation_cwd,
            repo_root_override=self.root,
        )
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        detected_workspace_change = (
            refreshed_workspace_fingerprint != previous_workspace_fingerprint
        )
        workspace_changed = force or detected_workspace_change
        if detected_workspace_change and not force:
            self._workspace_snapshot_cache = None
            self._workspace_content_fingerprint_cache = None
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
        return f"{self.memory.render_panel()}\n\n{self.project_memory.index_text()}"

    def select_memory_for_task(self, user_message):
        if self._task_memory_selection is None:
            manifest = self.project_memory.selector_manifest()
            selector = getattr(self.model_client, "select_memory_filenames", None)
            filenames = []
            status = "empty" if not manifest else "unavailable"
            failure = {}
            if manifest and callable(selector):
                try:
                    filenames = selector(
                        user_message,
                        manifest,
                        max_files=MEMORY_SELECTOR_MAX_SELECTED,
                        max_new_tokens=192,
                    )
                    status = "available"
                except Exception as exc:  # noqa: BLE001 - model selector is an optional boundary
                    failure = {"code": "memory_selector_failed", "detail": clip(str(exc), 300)}
            cards = self.project_memory.selected_cards(filenames)
            self._task_memory_selection = {
                "query": str(user_message),
                "cards": cards,
                "status": status,
                "failure": failure,
                "available_count": len(manifest),
            }
        selected = self._task_memory_selection
        working_text, working_metadata = self.memory.render_recall(selected["query"])
        project_text = self.project_memory.render_selected(selected["cards"])
        return f"{working_text}\n\n{self.project_memory.index_text()}\n\n{project_text}", {
            "status": selected["status"],
            "failure": dict(selected["failure"]),
            "available_count": selected["available_count"],
            "selected_filenames": [card.filename for card in selected["cards"]],
            **working_metadata,
        }

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
        payload = dict(item)
        task_state = getattr(self, "current_task_state", None)
        if task_state is not None:
            payload.setdefault("run_id", task_state.run_id)
        self.session["history"].append(payload)
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return securitylib.is_secret_env_name(name, secret_env_names=self.secret_env_names)

    def configured_secret_env_items(self):
        return securitylib.configured_secret_env_items(secret_env_names=self.secret_env_names)

    def detected_secret_env_items(self):
        return securitylib.detected_secret_env_items(secret_env_names=self.secret_env_names)

    def secret_env_summary(self):
        return securitylib.secret_env_summary(secret_env_names=self.secret_env_names)

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(secret_env_names=self.secret_env_names)

    def redact_text(self, text):
        return securitylib.redact_text(text, secret_env_names=self.secret_env_names)

    def redact_artifact(self, value, key=None):
        return securitylib.redact_artifact(value, key=key, secret_env_names=self.secret_env_names)

    def shell_env(self):
        return securitylib.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 event/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_tokens": self.context_manager.tokenizer.count(self.prefix),
                "workspace_tokens": self.context_manager.tokenizer.count(self.workspace.text()),
                "memory_tokens": self.context_manager.tokenizer.count(self.memory_text()),
                "history_tokens": self.context_manager.tokenizer.count(self.history_text()),
                "request_tokens": self.context_manager.tokenizer.count(user_message),
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
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

    def emit_event(self, task_state, event_type, payload=None, *, correlation_id=""):
        """Persist one redacted event through the only Runtime event boundary."""
        payload = self.redact_artifact(payload or {})
        correlation_id = str(correlation_id or payload.get("tool_call_id", ""))
        run_id = task_state.run_id if task_state is not None else "manual"
        task_id = task_state.task_id if task_state is not None else "manual"
        return self.run_store.append_event(
            run_id,
            task_id,
            event_type,
            payload,
            correlation_id=correlation_id,
            workspace_fingerprint=self.workspace.fingerprint(),
        )

    def _scan_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                snapshot[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:  # noqa: BLE001, S112 - files may disappear during a live scan
                continue
        return snapshot

    def capture_workspace_snapshot(self, *, force=False):
        if force or self._workspace_snapshot_cache is None:
            self._workspace_snapshot_cache = self._scan_workspace_snapshot()
            self._workspace_content_fingerprint_cache = None
        return self._workspace_snapshot_cache

    def content_workspace_fingerprint(self):
        if self._workspace_content_fingerprint_cache is None:
            payload = json.dumps(
                self.capture_workspace_snapshot(), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self._workspace_content_fingerprint_cache = hashlib.sha256(payload).hexdigest()
        return self._workspace_content_fingerprint_cache

    def run_verification(self):
        return run_verification(self)

    @staticmethod
    def diff_workspace_snapshots(before, after):
        changed_paths = []
        summaries = []
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            if before.get(path) == after.get(path):
                continue
            changed_paths.append(path)
            if path not in before:
                summaries.append(f"created:{path}")
            elif path not in after:
                summaries.append(f"deleted:{path}")
            else:
                summaries.append(f"modified:{path}")
        return changed_paths, summaries

    def create_checkpoint(self, task_state, user_message, trigger):
        return checkpointlib.create_checkpoint(self, task_state, user_message, trigger)

    def infer_next_step(self, task_state):
        return checkpointlib.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result, outcome=None):
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
            self.memory.set_file_observation(
                canonical_path,
                summary,
                source_session_id=self.session["id"],
                source_run_id=str(getattr(self.current_task_state, "run_id", "")),
                source_tool_call_id=str(getattr(outcome, "tool_call_id", "")),
                source_artifact_id=str(getattr(outcome, "artifact_id", "")),
            )
        elif name in {"write_file", "patch_file"}:
            self.memory.invalidate_file_observation(canonical_path)

    def ask(self, user_message):
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    def run_tool(self, call_or_name, args=None):
        """Execute through the single admission boundary and return ToolOutcome."""
        call = (
            call_or_name
            if isinstance(call_or_name, ToolCall)
            else ToolCall(str(call_or_name), dict(args or {}))
        )
        self.last_tool_outcome = self.tool_executor.execute(call)
        return self.last_tool_outcome

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        # report 是一次运行的最终摘要；
        # Event log 关注过程，report 是从过程投影出的终态和关键指标。
        event_summary = self.run_store.replay(task_state.run_id).summary()
        for field in (
            "run_id", "task_id", "status", "stop_reason", "attempts",
            "tool_steps", "last_tool", "checkpoint_id",
        ):
            event_summary.pop(field, None)
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "prompt_metadata": self.last_prompt_metadata,
            "project_memory": {"count": self.project_memory.count()},
            "evidence": self.evidence_ledger.to_dict(),
            "event_summary": event_summary,
            "redacted_env": self.detected_secret_env_summary(),
        }

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        return toolkit.validate_tool(self.tool_context(), name, args)

    def tool_context(self):
        def pending_call_entry_ids():
            ledger = getattr(self, "context_ledger", None)
            if ledger is None:
                return ()
            call_id = ledger.pending_call_id()
            return tuple(
                entry.entry_id
                for entry in ledger.entries
                if entry.kind == "assistant_tool_call" and entry.call_id == call_id
            )

        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            project_memory=self.project_memory,
            artifact_store=self.artifact_store,
            session_id=self.session["id"],
            run_id_provider=lambda: str(getattr(self.current_task_state, "run_id", "") or "manual"),
            source_entry_ids_provider=pending_call_entry_ids,
            tool_call_id_provider=lambda: (
                getattr(self, "context_ledger", None).pending_call_id()
                if getattr(self, "context_ledger", None) is not None
                else ""
            ),
            repo_map=self.repo_map,
            mutation_service=self.mutation_service,
            sandbox=self.sandbox,
            execution_context_provider=lambda: (
                self.current_execution.child(owner="run_shell")
                if self.current_execution is not None else None
            ),
        )

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
        self.memory = memorylib.SessionWorkingMemory(self.session["memory"], workspace_root=self.root)
        self._task_memory_selection = None
        self.session_store.save(self.session)

    def cancel_current_run(self, reason="user_cancelled"):
        if self.current_execution is None:
            return False
        self.current_execution.request_stop(reason)
        return True

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved
