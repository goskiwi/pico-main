"""Real-model coding benchmark with hidden, Docker-isolated verification."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from .common import git_value as _git_value
from .common import safe_mean as _safe_mean
from .common import safe_ratio as _safe_ratio
from .common import utc_timestamp as _utc_timestamp
from pico.models import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from pico.run_store import RunStore
from pico.runtime import Pico
from pico.sandbox import DockerSandbox, DockerSandboxConfig
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


REAL_BENCHMARK_SCHEMA_VERSION = 1
REAL_BENCHMARK_ARTIFACT_SCHEMA_VERSION = 2
DEFAULT_REAL_BENCHMARK_PATH = Path("benchmarks/real_world_tasks.json")
DEFAULT_REAL_ARTIFACT_PATH = Path("artifacts/real-world-benchmark-v1-structured.json")
DEFAULT_REAL_REPORT_PATH = Path("docs/metrics/real-world-benchmark-v1-structured.md")
DEFAULT_REAL_WORKSPACE_ROOT = Path("artifacts/real-world-workspaces")
REQUIRED_TASK_KEYS = (
    "id",
    "category",
    "prompt",
    "fixture_repo",
    "allowed_tools",
    "step_budget",
    "verifier_files",
    "verifier_command",
)
VARIANT_FULL = "full"
VARIANT_NO_MEMORY_CONTEXT = "no_memory_context"
SUPPORTED_VARIANTS = (VARIANT_FULL, VARIANT_NO_MEMORY_CONTEXT)


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
    if int(payload.get("schema_version", 0)) != REAL_BENCHMARK_SCHEMA_VERSION:
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
        missing = [key for key in REQUIRED_TASK_KEYS if key not in raw_task]
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
        if not task["prompt"] or not task["category"]:
            raise ValueError(f"task {task_id} prompt and category must not be empty")
        if task["step_budget"] < 1:
            raise ValueError(f"task {task_id} step_budget must be positive")
        if not task["allowed_tools"] or any(not name for name in task["allowed_tools"]):
            raise ValueError(f"task {task_id} allowed_tools must not be empty")
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


def load_real_benchmark(path=DEFAULT_REAL_BENCHMARK_PATH, repo_root=None):
    path = Path(path).resolve()
    root = Path(repo_root).resolve() if repo_root else path.parent.parent
    return validate_real_benchmark(json.loads(path.read_text(encoding="utf-8")), root)


def build_real_model_client(provider, model, base_url=None, timeout=300):
    provider = str(provider).strip().lower()
    model = str(model).strip()
    if not model:
        raise ValueError("model must not be empty")
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required for the real benchmark")
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url
            or os.environ.get("OPENAI_API_BASE")
            or "https://api.openai.com/v1",
            api_key=api_key,
            temperature=0.0,
            timeout=int(timeout),
        )
    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for the real benchmark")
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url
            or os.environ.get("ANTHROPIC_API_BASE")
            or "https://api.anthropic.com/v1",
            api_key=api_key,
            temperature=0.0,
            timeout=int(timeout),
        )
    raise ValueError("provider must be 'openai' or 'anthropic'")


def _variant_feature_flags(variant):
    if variant == VARIANT_FULL:
        return {
            "llm_memory_extract": False,
            "require_explicit_final": True,
            "require_workspace_change": True,
        }
    if variant == VARIANT_NO_MEMORY_CONTEXT:
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


def _trace_metrics(trace_path):
    events = []
    if Path(trace_path).is_file():
        events = [
            json.loads(line)
            for line in Path(trace_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    requested_events = [
        event for event in events if event.get("event") == "model_requested"
    ]
    model_events = [event for event in events if event.get("event") == "model_parsed"]
    failed_events = [event for event in events if event.get("event") == "model_failed"]
    rejected_events = [
        event for event in events if event.get("event") == "model_action_rejected"
    ]
    input_tokens = sum(
        int((event.get("completion_metadata") or {}).get("input_tokens") or 0)
        for event in model_events
    )
    output_tokens = sum(
        int((event.get("completion_metadata") or {}).get("output_tokens") or 0)
        for event in model_events
    )
    cached_tokens = sum(
        int((event.get("completion_metadata") or {}).get("cached_tokens") or 0)
        for event in model_events
    )
    action_protocols = sorted(
        {
            str(event.get("action_protocol", "")).strip()
            for event in model_events
            if str(event.get("action_protocol", "")).strip()
        }
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "model_calls": len(requested_events),
        "model_failures": len(failed_events),
        "model_action_rejections": len(rejected_events),
        "action_protocols": action_protocols,
    }


def _failure_category(task_state, verifier_result, report):
    if str(task_state.status) == "failed":
        return "model_error"
    if str(task_state.stop_reason) in {"step_limit_reached", "retry_limit_reached"}:
        return str(task_state.stop_reason)
    if verifier_result.timed_out:
        return "verifier_timeout"
    if verifier_result.returncode != 0:
        if not (report.get("summary") or {}).get("changed_files"):
            return "no_effective_change"
        return "verifier_failed"
    return ""


def summarize_real_rows(rows):
    rows = list(rows)
    variants = {}
    for variant in SUPPORTED_VARIANTS:
        variant_rows = [row for row in rows if row["variant"] == variant]
        if not variant_rows:
            continue
        repetition_summaries = []
        for repetition in sorted(
            {int(row.get("repetition", 1)) for row in variant_rows}
        ):
            repetition_rows = [
                row
                for row in variant_rows
                if int(row.get("repetition", 1)) == repetition
            ]
            passed = sum(1 for row in repetition_rows if row["passed"])
            repetition_summaries.append(
                {
                    "repetition": repetition,
                    "attempt_count": len(repetition_rows),
                    "passed": passed,
                    "pass_rate": _safe_ratio(passed, len(repetition_rows)),
                    "avg_tool_steps": _safe_mean(
                        row["tool_steps"] for row in repetition_rows
                    ),
                    "avg_model_calls": _safe_mean(
                        row["model_calls"] for row in repetition_rows
                    ),
                    "avg_total_duration_ms": _safe_mean(
                        row["total_duration_ms"] for row in repetition_rows
                    ),
                }
            )
        task_stability = []
        for task_id in sorted({str(row["task_id"]) for row in variant_rows}):
            task_rows = [row for row in variant_rows if str(row["task_id"]) == task_id]
            passed = sum(1 for row in task_rows if row["passed"])
            if passed == len(task_rows):
                outcome = "always_passed"
            elif passed == 0:
                outcome = "always_failed"
            else:
                outcome = "mixed"
            task_stability.append(
                {
                    "task_id": task_id,
                    "category": task_rows[0]["category"],
                    "attempt_count": len(task_rows),
                    "passed": passed,
                    "pass_rate": _safe_ratio(passed, len(task_rows)),
                    "outcome": outcome,
                }
            )
        repetition_pass_rates = [item["pass_rate"] for item in repetition_summaries]
        passed = sum(1 for row in variant_rows if row["passed"])
        variants[variant] = {
            "task_count": len(task_stability),
            "attempt_count": len(variant_rows),
            "repetition_count": len(repetition_summaries),
            "passed": passed,
            "pass_rate": _safe_ratio(passed, len(variant_rows)),
            "repetition_pass_rate_mean": _safe_mean(repetition_pass_rates),
            "repetition_pass_rate_stddev": (
                statistics.pstdev(repetition_pass_rates)
                if len(repetition_pass_rates) > 1
                else 0.0
            ),
            "repetition_pass_rate_min": min(repetition_pass_rates),
            "repetition_pass_rate_max": max(repetition_pass_rates),
            "complete_repetitions": sum(
                1
                for item in repetition_summaries
                if item["passed"] == item["attempt_count"]
            ),
            "repetition_summaries": repetition_summaries,
            "task_stability": task_stability,
            "avg_tool_steps": _safe_mean(row["tool_steps"] for row in variant_rows),
            "avg_model_calls": _safe_mean(row["model_calls"] for row in variant_rows),
            "avg_model_action_rejections": _safe_mean(
                row.get("model_action_rejections", 0) for row in variant_rows
            ),
            "avg_agent_duration_ms": _safe_mean(
                row["agent_duration_ms"] for row in variant_rows
            ),
            "avg_total_duration_ms": _safe_mean(
                row["total_duration_ms"] for row in variant_rows
            ),
            "total_input_tokens": sum(row["input_tokens"] for row in variant_rows),
            "total_output_tokens": sum(row["output_tokens"] for row in variant_rows),
            "total_cached_tokens": sum(row["cached_tokens"] for row in variant_rows),
            "action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in row.get("action_protocols", [])
                }
            ),
        }
    category_task_ids = {}
    failure_counts = {}
    for row in rows:
        category_task_ids.setdefault(row["category"], set()).add(str(row["task_id"]))
        if row["failure_category"]:
            failure_counts[row["failure_category"]] = (
                failure_counts.get(row["failure_category"], 0) + 1
            )
    comparison = {}
    if VARIANT_FULL in variants and VARIANT_NO_MEMORY_CONTEXT in variants:
        comparison = {
            "pass_rate_delta": variants[VARIANT_FULL]["pass_rate"]
            - variants[VARIANT_NO_MEMORY_CONTEXT]["pass_rate"],
            "avg_tool_steps_delta": variants[VARIANT_FULL]["avg_tool_steps"]
            - variants[VARIANT_NO_MEMORY_CONTEXT]["avg_tool_steps"],
        }
    return {
        "row_count": len(rows),
        "category_counts": {
            category: len(task_ids)
            for category, task_ids in sorted(category_task_ids.items())
        },
        "failure_category_counts": failure_counts,
        "variants": variants,
        "comparison": comparison,
    }


@dataclass
class RealWorldBenchmarkRunner:
    benchmark_path: Path = DEFAULT_REAL_BENCHMARK_PATH
    artifact_path: Path = DEFAULT_REAL_ARTIFACT_PATH
    report_path: Path = DEFAULT_REAL_REPORT_PATH
    workspace_root: Path = DEFAULT_REAL_WORKSPACE_ROOT
    provider: str = "openai"
    model: str = ""
    base_url: str | None = None
    variants: tuple[str, ...] = (VARIANT_FULL,)
    repetitions: int = 1
    max_new_tokens: int = 1024
    verifier_timeout: int = 90
    require_clean_worktree: bool = False
    sandbox_config: DockerSandboxConfig | None = None

    def __post_init__(self):
        self.benchmark_path = Path(self.benchmark_path).resolve()
        self.repo_root = self.benchmark_path.parent.parent
        self.artifact_path = Path(self.artifact_path)
        self.report_path = Path(self.report_path)
        self.workspace_root = Path(self.workspace_root)
        self.variants = tuple(str(value) for value in self.variants)
        if not self.variants or any(
            value not in SUPPORTED_VARIANTS for value in self.variants
        ):
            raise ValueError(
                f"variants must be drawn from: {', '.join(SUPPORTED_VARIANTS)}"
            )
        if len(set(self.variants)) != len(self.variants):
            raise ValueError("variants must not contain duplicates")
        if int(self.repetitions) < 1:
            raise ValueError("repetitions must be positive")
        if not str(self.model).strip():
            raise ValueError("model is required for a real benchmark")

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
        summary = summarize_real_rows(rows)
        git_status = _git_value(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            fallback=None,
        )
        artifact = {
            "schema_version": REAL_BENCHMARK_ARTIFACT_SCHEMA_VERSION,
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
            },
            "sandbox": (self.sandbox_config or DockerSandboxConfig()).__dict__,
            "summary": summary,
            "rows": rows,
        }
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        report = render_real_benchmark_markdown(artifact)
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
        agent = Pico(
            model_client=model_client,
            workspace=workspace,
            session_store=SessionStore(workspace_root / ".pico" / "sessions"),
            run_store=RunStore(workspace_root / ".pico" / "runs"),
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
        verifier_started = time.monotonic()
        verifier_result = self._verify(task, workspace_root, sandbox)
        verifier_duration_ms = int((time.monotonic() - verifier_started) * 1000)
        trace = _trace_metrics(agent.run_store.trace_path(task_state))
        passed = task_state.status == "completed" and verifier_result.returncode == 0
        failure_category = _failure_category(task_state, verifier_result, report)
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
            "model_calls": int(trace["model_calls"]),
            "model_failures": int(trace["model_failures"]),
            "model_action_rejections": int(trace["model_action_rejections"]),
            "action_protocols": list(trace["action_protocols"]),
            "input_tokens": int(trace["input_tokens"]),
            "output_tokens": int(trace["output_tokens"]),
            "cached_tokens": int(trace["cached_tokens"]),
            "agent_duration_ms": agent_duration_ms,
            "verifier_duration_ms": verifier_duration_ms,
            "total_duration_ms": agent_duration_ms + verifier_duration_ms,
            "changed_files": list(summary.get("changed_files") or []),
            "security_events": list(summary.get("security_events") or []),
            "workspace": str(relative_workspace),
            "run_id": task_state.run_id,
            "final_answer": str(final_answer),
            "verifier": {
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
            )
            if git_status is None:
                raise RuntimeError("cannot verify that the benchmark worktree is clean")
            if git_status:
                raise RuntimeError("benchmark requires a clean git worktree")
        self._shared_model_client = build_real_model_client(
            self.provider,
            self.model,
            self.base_url,
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


def render_real_benchmark_markdown(artifact):
    summary = artifact["summary"]
    benchmark_name = artifact["benchmark"].get("name") or "Pico Real-world Benchmark"
    lines = [
        f"# {benchmark_name}",
        "",
        f"- Captured at: `{artifact['captured_at']}`",
        f"- Provider: `{artifact['provider']}`",
        f"- Model: `{artifact['model']}`",
        f"- Execution mode: `{artifact.get('execution_mode', 'unknown')}`",
        f"- Commit: `{artifact['runtime']['commit_sha'] or 'working-tree'}`",
        f"- Working tree dirty: `{artifact['runtime'].get('working_tree_dirty', 'unknown')}`",
        f"- Tasks: {artifact['benchmark']['task_count']}",
        f"- Repetitions: {artifact['repetitions']}",
        f"- Fixture snapshot: `{artifact['benchmark']['fixture_snapshot_id']}`",
        f"- Evaluation snapshot: `{artifact['benchmark'].get('evaluation_snapshot_id', 'not-recorded')}`",
        (
            f"- Run config: temperature={artifact.get('run_config', {}).get('temperature', 'unknown')}, "
            f"max_new_tokens={artifact.get('run_config', {}).get('max_new_tokens', 'unknown')}, "
            f"verifier_timeout={artifact.get('run_config', {}).get('verifier_timeout_seconds', 'unknown')}s"
        ),
        (
            f"- Sandbox: `{artifact['sandbox']['image']}`, {artifact['sandbox']['cpus']} CPU, "
            f"{artifact['sandbox']['memory']} memory, {artifact['sandbox']['pids_limit']} PIDs"
        ),
        "",
        "## Results",
        "",
        "| Variant | Protocol | Pass rate | Passed | Avg tools | Avg calls | Action rejects | Input tokens | Cached | Output | Avg duration |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, metrics in summary["variants"].items():
        lines.append(
            f"| {variant} | {', '.join(metrics.get('action_protocols', [])) or '-'} "
            f"| {metrics['pass_rate']:.1%} | {metrics['passed']}/{metrics.get('attempt_count', metrics['task_count'])} "
            f"| {metrics['avg_tool_steps']:.2f} | {metrics['avg_model_calls']:.2f} "
            f"| {metrics.get('avg_model_action_rejections', 0):.2f} "
            f"| {metrics['total_input_tokens']} | {metrics['total_cached_tokens']} "
            f"| {metrics['total_output_tokens']} "
            f"| {metrics['avg_total_duration_ms'] / 1000:.2f}s |"
        )
    if artifact.get("repetitions", 1) > 1:
        lines.extend(
            [
                "",
                "## Repetition stability",
                "",
                "| Variant | Mean pass rate | Stddev | Min | Max | Complete runs |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for variant, metrics in summary["variants"].items():
            lines.append(
                f"| {variant} | {metrics['repetition_pass_rate_mean']:.1%} "
                f"| {metrics['repetition_pass_rate_stddev']:.1%} "
                f"| {metrics['repetition_pass_rate_min']:.1%} "
                f"| {metrics['repetition_pass_rate_max']:.1%} "
                f"| {metrics['complete_repetitions']}/{metrics['repetition_count']} |"
            )
        lines.extend(
            [
                "",
                "### Per repetition",
                "",
                "| Variant | Repetition | Pass rate | Passed | Avg calls | Avg duration |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for variant, metrics in summary["variants"].items():
            for item in metrics["repetition_summaries"]:
                lines.append(
                    f"| {variant} | {item['repetition']} | {item['pass_rate']:.1%} "
                    f"| {item['passed']}/{item['attempt_count']} "
                    f"| {item['avg_model_calls']:.2f} "
                    f"| {item['avg_total_duration_ms'] / 1000:.2f}s |"
                )
        lines.extend(
            [
                "",
                "### Per-task stability",
                "",
                "| Variant | Task | Pass rate | Passed | Outcome |",
                "|---|---|---:|---:|---|",
            ]
        )
        for variant, metrics in summary["variants"].items():
            for item in metrics["task_stability"]:
                lines.append(
                    f"| {variant} | {item['task_id']} | {item['pass_rate']:.1%} "
                    f"| {item['passed']}/{item['attempt_count']} | {item['outcome']} |"
                )
    if summary["comparison"]:
        lines.extend(
            [
                "",
                "## Ablation",
                "",
                f"- Pass-rate delta (full - no_memory_context): {summary['comparison']['pass_rate_delta']:+.1%}",
                f"- Avg tool-step delta: {summary['comparison']['avg_tool_steps_delta']:+.2f}",
            ]
        )
    if summary["failure_category_counts"]:
        lines.extend(
            [
                "",
                "## Failure breakdown",
                "",
                "| Failure category | Count |",
                "|---|---:|",
            ]
        )
        for category, count in sorted(summary["failure_category_counts"].items()):
            lines.append(f"| {category} | {count} |")
    lines.extend(
        [
            "",
            "## Task details",
            "",
            "| Task | Rep | Category | Variant | Result | Tools | Calls | Rejects | Duration | Failure |",
            "|---|---:|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in artifact["rows"]:
        result = "PASS" if row["passed"] else "FAIL"
        lines.append(
            f"| {row['task_id']} | {row.get('repetition', 1)} | {row['category']} | {row['variant']} | {result} "
            f"| {row['tool_steps']} | {row['model_calls']} | "
            f"{row.get('model_action_rejections', 0)} | "
            f"{row['total_duration_ms'] / 1000:.2f}s "
            f"| {row['failure_category'] or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Scope boundary",
            "",
            "- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.",
            "- Verifiers run inside the mandatory Docker sandbox with networking disabled.",
            "- Results are model-, prompt-, and fixture-snapshot-specific; they are not a universal coding benchmark claim.",
            "- Repeated attempts over the same tasks are not independent task samples; standard deviation is calculated across full-suite repetitions.",
            "",
        ]
    )
    return "\n".join(lines)


def compare_real_benchmark_artifacts(baseline, candidate):
    """Compare two runs over the exact same benchmark snapshot and task set."""
    baseline = _load_artifact_value(baseline)
    candidate = _load_artifact_value(candidate)
    baseline_benchmark = baseline.get("benchmark") or {}
    candidate_benchmark = candidate.get("benchmark") or {}
    baseline_snapshot = baseline_benchmark.get(
        "evaluation_snapshot_id"
    ) or baseline_benchmark.get("fixture_snapshot_id")
    candidate_snapshot = candidate_benchmark.get(
        "evaluation_snapshot_id"
    ) or candidate_benchmark.get("fixture_snapshot_id")
    if not baseline_snapshot or baseline_snapshot != candidate_snapshot:
        raise ValueError("benchmark evaluation snapshots do not match")
    if baseline.get("provider") != candidate.get("provider"):
        raise ValueError("benchmark providers do not match")
    if baseline.get("model") != candidate.get("model"):
        raise ValueError("benchmark models do not match")
    baseline_rows = _full_rows_by_task(baseline)
    candidate_rows = _full_rows_by_task(candidate)
    if set(baseline_rows) != set(candidate_rows):
        raise ValueError("benchmark task sets do not match")

    task_rows = []
    for task_id in sorted(baseline_rows):
        before = baseline_rows[task_id]
        after = candidate_rows[task_id]
        task_rows.append(
            {
                "task_id": task_id,
                "baseline_passed": bool(before["passed"]),
                "candidate_passed": bool(after["passed"]),
                "pass_change": int(bool(after["passed"])) - int(bool(before["passed"])),
                "baseline_model_calls": int(before["model_calls"]),
                "candidate_model_calls": int(after["model_calls"]),
                "model_calls_delta": int(after["model_calls"])
                - int(before["model_calls"]),
                "baseline_action_rejections": (
                    int(before["model_action_rejections"])
                    if "model_action_rejections" in before
                    else None
                ),
                "candidate_action_rejections": (
                    int(after["model_action_rejections"])
                    if "model_action_rejections" in after
                    else None
                ),
            }
        )
    count = len(task_rows)
    baseline_passed = sum(row["baseline_passed"] for row in task_rows)
    candidate_passed = sum(row["candidate_passed"] for row in task_rows)
    return {
        "schema_version": 1,
        "artifact_type": "real-world-benchmark-comparison",
        "captured_at": _utc_timestamp(),
        "provider": baseline.get("provider", ""),
        "model": baseline.get("model", ""),
        "evaluation_snapshot_id": baseline_snapshot,
        "snapshot_type": (
            "evaluation"
            if baseline_benchmark.get("evaluation_snapshot_id")
            else "fixture_legacy"
        ),
        "task_count": count,
        "summary": {
            "baseline_pass_rate": _safe_ratio(baseline_passed, count),
            "candidate_pass_rate": _safe_ratio(candidate_passed, count),
            "pass_rate_delta": _safe_ratio(candidate_passed - baseline_passed, count),
            "baseline_avg_model_calls": _safe_mean(
                row["baseline_model_calls"] for row in task_rows
            ),
            "candidate_avg_model_calls": _safe_mean(
                row["candidate_model_calls"] for row in task_rows
            ),
            "avg_model_calls_delta": _safe_mean(
                row["model_calls_delta"] for row in task_rows
            ),
            "baseline_action_rejections": _optional_sum(
                row["baseline_action_rejections"] for row in task_rows
            ),
            "candidate_action_rejections": _optional_sum(
                row["candidate_action_rejections"] for row in task_rows
            ),
        },
        "rows": task_rows,
    }


def _load_artifact_value(value):
    if isinstance(value, dict):
        return value
    return json.loads(Path(value).read_text(encoding="utf-8"))


def _full_rows_by_task(artifact):
    full_rows = [
        row for row in artifact.get("rows", []) if row.get("variant") == VARIANT_FULL
    ]
    if any(int(row.get("repetition", 1)) != 1 for row in full_rows):
        raise ValueError("comparison accepts only single-repetition artifacts")
    rows = full_rows
    result = {str(row["task_id"]): row for row in rows}
    if len(result) != len(rows) or not result:
        raise ValueError("comparison needs one full-variant row per task")
    return result


def _optional_sum(values):
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def render_real_benchmark_comparison_markdown(comparison):
    summary = comparison["summary"]
    baseline_rejections = summary["baseline_action_rejections"]
    candidate_rejections = summary["candidate_action_rejections"]
    rejection_delta = (
        f"{candidate_rejections - baseline_rejections:+d}"
        if baseline_rejections is not None and candidate_rejections is not None
        else "n/a"
    )
    lines = [
        "# Structured Action Protocol Comparison",
        "",
        f"- Captured at: `{comparison['captured_at']}`",
        f"- Provider: `{comparison.get('provider', 'not-recorded')}`",
        f"- Model: `{comparison['model']}`",
        f"- Matched tasks: {comparison['task_count']}",
        f"- Snapshot ({comparison.get('snapshot_type', 'unknown')}): `{comparison['evaluation_snapshot_id']}`",
        "",
        "| Metric | Text protocol | Structured actions | Delta |",
        "|---|---:|---:|---:|",
        f"| Pass rate | {summary['baseline_pass_rate']:.1%} | {summary['candidate_pass_rate']:.1%} | {summary['pass_rate_delta']:+.1%} |",
        f"| Avg model calls | {summary['baseline_avg_model_calls']:.2f} | {summary['candidate_avg_model_calls']:.2f} | {summary['avg_model_calls_delta']:+.2f} |",
        f"| Action rejections | {baseline_rejections if baseline_rejections is not None else 'not recorded'} "
        f"| {candidate_rejections if candidate_rejections is not None else 'not recorded'} "
        f"| {rejection_delta} |",
        "",
        "## Task details",
        "",
        "| Task | Text | Structured | Calls before | Calls after |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in comparison["rows"]:
        lines.append(
            f"| {row['task_id']} | {'PASS' if row['baseline_passed'] else 'FAIL'} "
            f"| {'PASS' if row['candidate_passed'] else 'FAIL'} "
            f"| {row['baseline_model_calls']} | {row['candidate_model_calls']} |"
        )
    lines.extend(
        [
            "",
            "The comparison is accepted only when provider, model, task IDs, and the full evaluation snapshot are identical.",
            "",
        ]
    )
    return "\n".join(lines)
