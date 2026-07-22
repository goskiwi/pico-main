"""Real-model coding benchmark with hidden, Docker-isolated verification."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import real_benchmark_contract as contract
from . import real_benchmark_evidence as evidence
from . import real_benchmark_reporting as reporting
from .common import git_value as _git_value
from .common import utc_timestamp as _utc_timestamp
from pico.cli import DEFAULT_OPENAI_MODEL, _load_workspace_env
from pico.models import OpenAICompatibleModelClient
from pico.run_store import RunStore
from pico.runtime import Pico
from pico.sandbox import DockerSandbox, DockerSandboxConfig, SandboxResult
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


def _relative_file(path, root, *, label):
    path = (Path(root) / str(path)).resolve()
    try:
        return path.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes its root: {path}") from exc


def _fixture_snapshot_id(tasks, repo_root):
    digest = hashlib.sha256()
    fixture_roots = sorted(
        {(Path(repo_root) / task["fixture_repo"]).resolve() for task in tasks},
        key=str,
    )
    for fixture_root in fixture_roots:
        for path in sorted(
            (item for item in fixture_root.rglob("*") if item.is_file()), key=str
        ):
            digest.update(fixture_root.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(path.relative_to(fixture_root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _evaluation_snapshot_id(benchmark, tasks, repo_root):
    """Hash every input that can change a benchmark outcome."""
    digest = hashlib.sha256()
    selected_tasks = sorted((dict(task) for task in tasks), key=lambda task: task["id"])
    digest.update(
        json.dumps(
            {
                "schema_version": benchmark["schema_version"],
                "tasks": selected_tasks,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(b"\0fixtures\0")
    digest.update(_fixture_snapshot_id(selected_tasks, repo_root).encode("ascii"))
    for task in selected_tasks:
        for verifier_file in sorted(
            task["verifier_files"], key=lambda item: (item["source"], item["target"])
        ):
            source_relative = _relative_file(
                verifier_file["source"], repo_root, label="verifier source"
            )
            source = Path(repo_root) / source_relative
            digest.update(b"\0verifier\0")
            digest.update(task["id"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(source_relative).encode("utf-8"))
            digest.update(b"\0")
            digest.update(source.read_bytes())
    return "sha256:" + digest.hexdigest()


def validate_real_benchmark(payload, repo_root):
    if not isinstance(payload, dict):
        raise ValueError("real benchmark must be an object")
    if int(payload.get("schema_version", 0)) != contract.REAL_BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported real benchmark schema_version")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("real benchmark tasks must be a non-empty list")

    repo_root = Path(repo_root).resolve()
    seen_ids = set()
    normalized = []
    for index, raw_task in enumerate(tasks):
        if not isinstance(raw_task, dict):
            raise ValueError(f"task at index {index} must be an object")
        missing = [key for key in contract.REQUIRED_TASK_KEYS if key not in raw_task]
        if missing:
            raise ValueError(
                f"task {raw_task.get('id', index)!r} missing: {', '.join(missing)}"
            )
        task = dict(raw_task)
        task_id = str(task["id"]).strip()
        if not task_id or task_id in seen_ids:
            raise ValueError(f"empty or duplicate task id: {task_id!r}")
        seen_ids.add(task_id)
        task["id"] = task_id
        task["category"] = str(task["category"]).strip()
        task["prompt"] = str(task["prompt"]).strip()
        task["fixture_repo"] = str(task["fixture_repo"]).strip()
        task["step_budget"] = int(task["step_budget"])
        task["verifier_command"] = str(task["verifier_command"]).strip()
        task["allowed_tools"] = [str(name).strip() for name in task["allowed_tools"]]
        task["verifier_files"] = [dict(item) for item in task["verifier_files"]]
        if "required_tools" in task:
            task["required_tools"] = [
                str(name).strip() for name in task["required_tools"]
            ]
        if "require_successful_delegates" in task:
            if not isinstance(task["require_successful_delegates"], bool):
                raise ValueError(
                    f"task {task_id} require_successful_delegates must be a boolean"
                )
            task["require_successful_delegates"] = bool(
                task["require_successful_delegates"]
            )
        if "expected_delegate_runs" in task:
            expected_delegate_runs = task["expected_delegate_runs"]
            if isinstance(expected_delegate_runs, bool) or not isinstance(
                expected_delegate_runs, int
            ):
                raise ValueError(
                    f"task {task_id} expected_delegate_runs must be an integer"
                )
            if expected_delegate_runs < 1:
                raise ValueError(
                    f"task {task_id} expected_delegate_runs must be positive"
                )
            if not task.get("require_successful_delegates", False):
                raise ValueError(
                    f"task {task_id} expected_delegate_runs requires "
                    "require_successful_delegates=true"
                )
        if "expected_delegate_attempts" in task:
            expected_delegate_attempts = task["expected_delegate_attempts"]
            if isinstance(expected_delegate_attempts, bool) or not isinstance(
                expected_delegate_attempts, int
            ):
                raise ValueError(
                    f"task {task_id} expected_delegate_attempts must be an integer"
                )
            if expected_delegate_attempts < 1:
                raise ValueError(
                    f"task {task_id} expected_delegate_attempts must be positive"
                )
            if not task.get("require_successful_delegates", False):
                raise ValueError(
                    f"task {task_id} expected_delegate_attempts requires "
                    "require_successful_delegates=true"
                )
        if not task["prompt"] or not task["category"]:
            raise ValueError(f"task {task_id} prompt and category must not be empty")
        if task["step_budget"] < 1:
            raise ValueError(f"task {task_id} step_budget must be positive")
        if not task["allowed_tools"] or any(not name for name in task["allowed_tools"]):
            raise ValueError(f"task {task_id} allowed_tools must not be empty")
        required_tools = task.get("required_tools", [])
        if any(not name for name in required_tools):
            raise ValueError(
                f"task {task_id} required_tools must not contain empty names"
            )
        if len(set(required_tools)) != len(required_tools):
            raise ValueError(
                f"task {task_id} required_tools must not contain duplicates"
            )
        unavailable_required_tools = sorted(
            set(required_tools) - set(task["allowed_tools"])
        )
        if unavailable_required_tools:
            raise ValueError(
                f"task {task_id} required_tools are not allowed: "
                f"{', '.join(unavailable_required_tools)}"
            )
        fixture_root = (repo_root / task["fixture_repo"]).resolve()
        if not fixture_root.is_dir():
            raise ValueError(
                f"task {task_id} fixture repo does not exist: {task['fixture_repo']}"
            )
        for verifier_file in task["verifier_files"]:
            if set(verifier_file) != {"source", "target"}:
                raise ValueError(
                    f"task {task_id} verifier file needs source and target"
                )
            source = repo_root / _relative_file(
                verifier_file["source"], repo_root, label="verifier source"
            )
            if not source.is_file():
                raise ValueError(
                    f"task {task_id} verifier source does not exist: {source}"
                )
            _relative_file(
                verifier_file["target"], fixture_root, label="verifier target"
            )
        normalized.append(task)

    result = dict(payload)
    result["tasks"] = normalized
    return result


def load_real_benchmark(path=contract.DEFAULT_REAL_BENCHMARK_PATH, repo_root=None):
    path = Path(path).resolve()
    root = Path(repo_root).resolve() if repo_root else path.parent.parent
    return validate_real_benchmark(json.loads(path.read_text(encoding="utf-8")), root)


def build_real_model_client(model, base_url=None, timeout=300, *, env):
    model = str(model).strip()
    if not model:
        raise ValueError("model must not be empty")
    api_key = env.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is required in the project .env.local for the real benchmark"
        )
    return OpenAICompatibleModelClient(
        model=model,
        base_url=base_url or env.get("OPENAI_API_BASE") or "https://api.openai.com/v1",
        api_key=api_key,
        temperature=0.0,
        timeout=int(timeout),
    )


def _variant_feature_flags(variant):
    if variant == contract.VARIANT_FULL:
        return {
            "llm_memory_extract": False,
            "require_explicit_final": True,
            "require_workspace_change": True,
        }
    if variant == contract.VARIANT_NO_MEMORY_CONTEXT:
        return {
            "memory": False,
            "relevant_memory": False,
            "context_reduction": False,
            "llm_memory_extract": False,
            "llm_history_compaction": False,
            "dynamic_budget": False,
            "cross_section_dedup": False,
            "require_explicit_final": True,
            "require_workspace_change": True,
        }
    raise ValueError(f"unsupported benchmark variant: {variant}")


@dataclass
class RealWorldBenchmarkRunner:
    benchmark_path: Path = contract.DEFAULT_REAL_BENCHMARK_PATH
    artifact_path: Path = contract.DEFAULT_REAL_ARTIFACT_PATH
    report_path: Path = contract.DEFAULT_REAL_REPORT_PATH
    workspace_root: Path = contract.DEFAULT_REAL_WORKSPACE_ROOT
    provider: str = "openai"
    model: str = ""
    base_url: str | None = None
    variants: tuple[str, ...] = (contract.VARIANT_FULL,)
    repetitions: int = 1
    max_new_tokens: int = 1024
    verifier_timeout: int = 90
    require_clean_worktree: bool = False
    sandbox_config: DockerSandboxConfig | None = None

    def __post_init__(self):
        self.provider = str(self.provider).strip().lower()
        if self.provider != "openai":
            raise ValueError("real benchmark provider must be 'openai'")
        self.benchmark_path = Path(self.benchmark_path).resolve()
        self.repo_root = self.benchmark_path.parent.parent
        workspace_env = _load_workspace_env(self.repo_root)
        self.artifact_path = Path(self.artifact_path)
        self.report_path = Path(self.report_path)
        self.workspace_root = Path(self.workspace_root)
        self.variants = tuple(str(value) for value in self.variants)
        if not self.variants or any(
            value not in contract.SUPPORTED_VARIANTS for value in self.variants
        ):
            raise ValueError(
                f"variants must be drawn from: {', '.join(contract.SUPPORTED_VARIANTS)}"
            )
        if len(set(self.variants)) != len(self.variants):
            raise ValueError("variants must not contain duplicates")
        if int(self.repetitions) < 1:
            raise ValueError("repetitions must be positive")
        self.model = str(
            self.model or workspace_env.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        ).strip()

    def run(self, task_ids=None):
        benchmark = load_real_benchmark(self.benchmark_path, self.repo_root)
        selected_ids = {str(task_id) for task_id in (task_ids or ())}
        tasks = [
            task
            for task in benchmark["tasks"]
            if not selected_ids or task["id"] in selected_ids
        ]
        unknown_ids = selected_ids - {task["id"] for task in tasks}
        if unknown_ids:
            raise ValueError(
                f"unknown benchmark task ids: {', '.join(sorted(unknown_ids))}"
            )
        self._preflight()
        rows = []
        for repetition in range(1, int(self.repetitions) + 1):
            for variant in self.variants:
                for task in tasks:
                    rows.append(
                        self.run_task(task, variant=variant, repetition=repetition)
                    )
        summary = reporting.summarize_real_rows(rows)
        git_status = _git_value(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            fallback=None,
            preserve_empty=True,
        )
        artifact = {
            "schema_version": contract.REAL_BENCHMARK_ARTIFACT_SCHEMA_VERSION,
            "artifact_type": "real-world-benchmark",
            "execution_mode": "live_llm",
            "captured_at": _utc_timestamp(),
            "runtime": {
                "commit_sha": _git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": _git_value(["branch", "--show-current"], cwd=self.repo_root),
                "working_tree_dirty": None if git_status is None else bool(git_status),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
            },
            "benchmark": {
                "name": benchmark.get("name", ""),
                "description": benchmark.get("description", ""),
                "source": str(self.benchmark_path.relative_to(self.repo_root)),
                "task_count": len(tasks),
                "task_ids": [task["id"] for task in tasks],
                "fixture_snapshot_id": _fixture_snapshot_id(tasks, self.repo_root),
                "evaluation_snapshot_id": _evaluation_snapshot_id(
                    benchmark, tasks, self.repo_root
                ),
            },
            "provider": self.provider,
            "model": self.model,
            "variants": list(self.variants),
            "repetitions": int(self.repetitions),
            "run_config": {
                "temperature": 0.0,
                "max_new_tokens": int(self.max_new_tokens),
                "verifier_timeout_seconds": int(self.verifier_timeout),
                "require_clean_worktree": bool(self.require_clean_worktree),
                "model_cost_scope": "attempt_parent_and_related_delegates",
                "model_duration_semantics": "sum_of_model_call_durations",
                "agent_duration_semantics": (
                    "parent_attempt_wall_clock_including_delegate_wait"
                ),
            },
            "sandbox": (self.sandbox_config or DockerSandboxConfig()).__dict__,
            "summary": summary,
            "rows": rows,
        }
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        report = reporting.render_real_benchmark_markdown(artifact)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(report, encoding="utf-8")
        return artifact

    def run_task(self, task, *, variant, repetition):
        fixture_source = (self.repo_root / task["fixture_repo"]).resolve()
        relative_workspace = (
            Path(f"rep-{repetition}") / variant / task["id"] / fixture_source.name
        )
        workspace_root = (self.workspace_root / relative_workspace).resolve()
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        workspace_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, workspace_root)

        workspace = WorkspaceContext.build(
            workspace_root, repo_root_override=workspace_root
        )
        model_client = self._shared_model_client
        sandbox = self._sandbox(workspace_root)
        run_store = RunStore(workspace_root / ".pico" / "runs")
        existing_run_ids = {
            path.name for path in run_store.root.glob("run_*") if path.is_dir()
        }
        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=SessionStore(workspace_root / ".pico" / "sessions"),
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=int(self.max_new_tokens),
            allowed_tools=tuple(task["allowed_tools"]),
            feature_flags=_variant_feature_flags(variant),
            sandbox=sandbox,
        )
        started = time.monotonic()
        final_answer = agent.ask(task["prompt"])
        agent_duration_ms = int((time.monotonic() - started) * 1000)
        task_state = agent.current_task_state
        report = agent.run_store.load_report(task_state.run_id)
        run_dirs = [
            path
            for path in run_store.root.glob("run_*")
            if path.is_dir() and path.name not in existing_run_ids
        ]
        attempt_trace = evidence._attempt_trace_metrics(
            run_store.run_dir(task_state),
            run_dirs,
            workspace_root,
        )
        trace = attempt_trace["parent"]
        workspace_isolation = evidence._workspace_isolation_audit(
            workspace_root,
            run_dirs,
            task,
        )
        verifier_started = time.monotonic()
        verifier_skipped = not workspace_isolation["ok"]
        if verifier_skipped:
            verifier_result = SandboxResult(
                returncode=125,
                stderr="verifier skipped: workspace isolation audit failed",
            )
        else:
            verifier_result = self._verify(task, workspace_root, sandbox)
        verifier_duration_ms = int((time.monotonic() - verifier_started) * 1000)
        required_tools = list(task.get("required_tools", []))
        missing_required_tools = sorted(
            set(required_tools) - set(trace["executed_tools"])
        )
        require_successful_delegates = bool(
            task.get("require_successful_delegates", False)
        )
        expected_delegate_runs = task.get("expected_delegate_runs")
        expected_delegate_attempts = task.get("expected_delegate_attempts")
        delegate_evidence = evidence._evaluate_delegate_evidence(
            trace,
            delegate_run_count=int(attempt_trace["delegate_run_count"]),
            delegate_agent_ids=attempt_trace["delegate_agent_ids"],
            required=require_successful_delegates,
            expected_delegate_runs=expected_delegate_runs,
            expected_delegate_attempts=expected_delegate_attempts,
        )
        failed_delegate_outcomes = list(delegate_evidence["issues"])
        delegate_outcomes_ok = bool(delegate_evidence["ok"])
        trace_parse_errors = list(attempt_trace["total"]["trace_parse_errors"])
        passed = (
            task_state.status == "completed"
            and workspace_isolation["ok"]
            and verifier_result.returncode == 0
            and not missing_required_tools
            and delegate_outcomes_ok
            and not trace_parse_errors
        )
        failure_category = evidence._failure_category(
            task_state,
            verifier_result,
            report,
            workspace_isolation_violations=workspace_isolation["violations"],
            missing_required_tools=missing_required_tools,
            failed_delegate_outcomes=(
                failed_delegate_outcomes if not delegate_outcomes_ok else ()
            ),
            trace_parse_errors=trace_parse_errors,
        )
        summary = dict(report.get("summary") or {})
        return {
            "task_id": task["id"],
            "category": task["category"],
            "variant": variant,
            "repetition": repetition,
            "passed": passed,
            "failure_category": failure_category,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "tool_steps": int(task_state.tool_steps),
            "parent_model_calls": int(attempt_trace["parent"]["model_calls"]),
            "delegate_model_calls": int(attempt_trace["delegate"]["model_calls"]),
            "total_model_calls": int(attempt_trace["total"]["model_calls"]),
            "model_calls": int(attempt_trace["total"]["model_calls"]),
            "parent_model_duration_ms": int(
                attempt_trace["parent"]["model_duration_ms"]
            ),
            "delegate_model_duration_ms": int(
                attempt_trace["delegate"]["model_duration_ms"]
            ),
            "total_model_duration_ms": int(attempt_trace["total"]["model_duration_ms"]),
            "parent_model_failures": int(attempt_trace["parent"]["model_failures"]),
            "delegate_model_failures": int(attempt_trace["delegate"]["model_failures"]),
            "total_model_failures": int(attempt_trace["total"]["model_failures"]),
            "model_failures": int(attempt_trace["total"]["model_failures"]),
            "parent_model_action_rejections": int(
                attempt_trace["parent"]["model_action_rejections"]
            ),
            "delegate_model_action_rejections": int(
                attempt_trace["delegate"]["model_action_rejections"]
            ),
            "total_model_action_rejections": int(
                attempt_trace["total"]["model_action_rejections"]
            ),
            "model_action_rejections": int(
                attempt_trace["total"]["model_action_rejections"]
            ),
            "parent_action_protocols": list(
                attempt_trace["parent"]["action_protocols"]
            ),
            "delegate_action_protocols": list(
                attempt_trace["delegate"]["action_protocols"]
            ),
            "total_action_protocols": list(attempt_trace["total"]["action_protocols"]),
            "action_protocols": list(attempt_trace["total"]["action_protocols"]),
            "executed_tools": list(trace["executed_tools"]),
            "required_tools": required_tools,
            "missing_required_tools": missing_required_tools,
            "require_successful_delegates": require_successful_delegates,
            "expected_delegate_runs": expected_delegate_runs,
            "expected_delegate_attempts": expected_delegate_attempts,
            "delegate_evidence": delegate_evidence,
            "failed_delegate_outcomes": failed_delegate_outcomes,
            "delegate_run_count": int(attempt_trace["delegate_run_count"]),
            "delegate_run_ids": list(attempt_trace["delegate_run_ids"]),
            "delegate_agent_ids": list(attempt_trace["delegate_agent_ids"]),
            "trace_parse_errors": trace_parse_errors,
            "parent_input_tokens": int(attempt_trace["parent"]["input_tokens"]),
            "delegate_input_tokens": int(attempt_trace["delegate"]["input_tokens"]),
            "total_input_tokens": int(attempt_trace["total"]["input_tokens"]),
            "input_tokens": int(attempt_trace["total"]["input_tokens"]),
            "parent_output_tokens": int(attempt_trace["parent"]["output_tokens"]),
            "delegate_output_tokens": int(attempt_trace["delegate"]["output_tokens"]),
            "total_output_tokens": int(attempt_trace["total"]["output_tokens"]),
            "output_tokens": int(attempt_trace["total"]["output_tokens"]),
            "parent_cached_tokens": int(attempt_trace["parent"]["cached_tokens"]),
            "delegate_cached_tokens": int(attempt_trace["delegate"]["cached_tokens"]),
            "total_cached_tokens": int(attempt_trace["total"]["cached_tokens"]),
            "cached_tokens": int(attempt_trace["total"]["cached_tokens"]),
            "agent_duration_ms": agent_duration_ms,
            "verifier_duration_ms": verifier_duration_ms,
            "total_duration_ms": agent_duration_ms + verifier_duration_ms,
            "changed_files": list(summary.get("changed_files") or []),
            "security_events": list(summary.get("security_events") or []),
            "workspace_isolation": workspace_isolation,
            "workspace": str(relative_workspace),
            "run_id": task_state.run_id,
            "final_answer": str(final_answer),
            "verifier": {
                "skipped": verifier_skipped,
                "exit_code": int(verifier_result.returncode),
                "timed_out": bool(verifier_result.timed_out),
                "stdout": verifier_result.stdout[-2000:],
                "stderr": verifier_result.stderr[-2000:],
            },
        }

    def _preflight(self):
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if self.require_clean_worktree:
            git_status = _git_value(
                ["status", "--porcelain", "--untracked-files=all"],
                cwd=self.repo_root,
                fallback=None,
                preserve_empty=True,
            )
            if git_status is None:
                raise RuntimeError("cannot verify that the benchmark worktree is clean")
            if git_status:
                raise RuntimeError("benchmark requires a clean git worktree")
        self._shared_model_client = build_real_model_client(
            self.model,
            self.base_url,
            env=_load_workspace_env(self.repo_root),
        )
        DockerSandbox(
            self.workspace_root,
            config=self.sandbox_config or DockerSandboxConfig(),
        ).ensure_ready()

    def _sandbox(self, workspace_root):
        return DockerSandbox(
            workspace_root, config=self.sandbox_config or DockerSandboxConfig()
        )

    def _verify(self, task, workspace_root, sandbox):
        installed = []
        try:
            for verifier_file in task["verifier_files"]:
                source = self.repo_root / _relative_file(
                    verifier_file["source"], self.repo_root, label="verifier source"
                )
                target_relative = _relative_file(
                    verifier_file["target"], workspace_root, label="verifier target"
                )
                target = workspace_root / target_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                installed.append(target)
            return sandbox.run(
                task["verifier_command"],
                cwd=workspace_root,
                timeout=int(self.verifier_timeout),
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            for path in installed:
                path.unlink(missing_ok=True)
            for directory in sorted(
                {path.parent for path in installed},
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
