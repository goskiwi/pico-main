"""Real-model coding benchmark with hidden, Docker-isolated verification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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
        for path in sorted((item for item in fixture_root.rglob("*") if item.is_file()), key=str):
            digest.update(fixture_root.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(path.relative_to(fixture_root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
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
            raise ValueError(f"task {raw_task.get('id', index)!r} missing: {', '.join(missing)}")
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
            raise ValueError(f"task {task_id} fixture repo does not exist: {task['fixture_repo']}")
        for verifier_file in task["verifier_files"]:
            if set(verifier_file) != {"source", "target"}:
                raise ValueError(f"task {task_id} verifier file needs source and target")
            source = repo_root / _relative_file(
                verifier_file["source"], repo_root, label="verifier source"
            )
            if not source.is_file():
                raise ValueError(f"task {task_id} verifier source does not exist: {source}")
            _relative_file(verifier_file["target"], fixture_root, label="verifier target")
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
            base_url=base_url or os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1",
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
            base_url=base_url or os.environ.get("ANTHROPIC_API_BASE") or "https://api.anthropic.com/v1",
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
    requested_events = [event for event in events if event.get("event") == "model_requested"]
    model_events = [event for event in events if event.get("event") == "model_parsed"]
    failed_events = [event for event in events if event.get("event") == "model_failed"]
    rejected_events = [event for event in events if event.get("event") == "model_action_rejected"]
    input_tokens = sum(int((event.get("completion_metadata") or {}).get("input_tokens") or 0) for event in model_events)
    output_tokens = sum(int((event.get("completion_metadata") or {}).get("output_tokens") or 0) for event in model_events)
    cached_tokens = sum(int((event.get("completion_metadata") or {}).get("cached_tokens") or 0) for event in model_events)
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
        variants[variant] = {
            "task_count": len(variant_rows),
            "passed": sum(1 for row in variant_rows if row["passed"]),
            "pass_rate": _safe_ratio(sum(1 for row in variant_rows if row["passed"]), len(variant_rows)),
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
    category_counts = {}
    failure_counts = {}
    for row in rows:
        category_counts[row["category"]] = category_counts.get(row["category"], 0) + 1
        if row["failure_category"]:
            failure_counts[row["failure_category"]] = failure_counts.get(row["failure_category"], 0) + 1
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
        "category_counts": category_counts,
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
    sandbox_config: DockerSandboxConfig | None = None
    model_client_factory: object | None = None
    sandbox_factory: object | None = None

    def __post_init__(self):
        self.benchmark_path = Path(self.benchmark_path).resolve()
        self.repo_root = self.benchmark_path.parent.parent
        self.artifact_path = Path(self.artifact_path)
        self.report_path = Path(self.report_path)
        self.workspace_root = Path(self.workspace_root)
        self.variants = tuple(str(value) for value in self.variants)
        if not self.variants or any(value not in SUPPORTED_VARIANTS for value in self.variants):
            raise ValueError(f"variants must be drawn from: {', '.join(SUPPORTED_VARIANTS)}")
        if len(set(self.variants)) != len(self.variants):
            raise ValueError("variants must not contain duplicates")
        if int(self.repetitions) < 1:
            raise ValueError("repetitions must be positive")
        if self.model_client_factory is None and not str(self.model).strip():
            raise ValueError("model is required for a real benchmark")

    def run(self, task_ids=None):
        benchmark = load_real_benchmark(self.benchmark_path, self.repo_root)
        selected_ids = {str(task_id) for task_id in (task_ids or ())}
        tasks = [task for task in benchmark["tasks"] if not selected_ids or task["id"] in selected_ids]
        unknown_ids = selected_ids - {task["id"] for task in tasks}
        if unknown_ids:
            raise ValueError(f"unknown benchmark task ids: {', '.join(sorted(unknown_ids))}")
        self._preflight()
        rows = []
        for repetition in range(1, int(self.repetitions) + 1):
            for variant in self.variants:
                for task in tasks:
                    rows.append(self.run_task(task, variant=variant, repetition=repetition))
        summary = summarize_real_rows(rows)
        artifact = {
            "schema_version": REAL_BENCHMARK_SCHEMA_VERSION,
            "artifact_type": "real-world-benchmark",
            "execution_mode": (
                "live_llm" if self.model_client_factory is None else "offline_harness_test"
            ),
            "captured_at": _utc_timestamp(),
            "runtime": {
                "commit_sha": _git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": _git_value(["branch", "--show-current"], cwd=self.repo_root),
            },
            "benchmark": {
                "name": benchmark.get("name", ""),
                "description": benchmark.get("description", ""),
                "source": str(self.benchmark_path.relative_to(self.repo_root)),
                "task_count": len(tasks),
                "fixture_snapshot_id": _fixture_snapshot_id(tasks, self.repo_root),
            },
            "provider": self.provider,
            "model": self.model,
            "variants": list(self.variants),
            "repetitions": int(self.repetitions),
            "sandbox": (self.sandbox_config or DockerSandboxConfig()).__dict__,
            "summary": summary,
            "rows": rows,
        }
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report = render_real_benchmark_markdown(artifact)
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(report, encoding="utf-8")
        return artifact

    def run_task(self, task, *, variant, repetition):
        fixture_source = (self.repo_root / task["fixture_repo"]).resolve()
        relative_workspace = Path(f"rep-{repetition}") / variant / task["id"] / fixture_source.name
        workspace_root = (self.workspace_root / relative_workspace).resolve()
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        workspace_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, workspace_root)

        workspace = WorkspaceContext.build(workspace_root, repo_root_override=workspace_root)
        model_client = self._model_client(task, variant)
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

    def _model_client(self, task, variant):
        if self.model_client_factory is not None:
            return self.model_client_factory(task=task, variant=variant)
        return self._shared_model_client

    def _preflight(self):
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if self.model_client_factory is None:
            self._shared_model_client = build_real_model_client(
                self.provider,
                self.model,
                self.base_url,
            )
        if self.sandbox_factory is None:
            DockerSandbox(
                self.workspace_root,
                config=self.sandbox_config or DockerSandboxConfig(),
            ).ensure_ready()

    def _sandbox(self, workspace_root):
        if self.sandbox_factory is not None:
            return self.sandbox_factory(workspace_root)
        return DockerSandbox(workspace_root, config=self.sandbox_config or DockerSandboxConfig())

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
                {path.parent for path in installed}, key=lambda path: len(path.parts), reverse=True
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
        f"- Tasks: {artifact['benchmark']['task_count']}",
        f"- Repetitions: {artifact['repetitions']}",
        f"- Fixture snapshot: `{artifact['benchmark']['fixture_snapshot_id']}`",
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
            f"| {metrics['pass_rate']:.1%} | {metrics['passed']}/{metrics['task_count']} "
            f"| {metrics['avg_tool_steps']:.2f} | {metrics['avg_model_calls']:.2f} "
            f"| {metrics.get('avg_model_action_rejections', 0):.2f} "
            f"| {metrics['total_input_tokens']} | {metrics['total_cached_tokens']} "
            f"| {metrics['total_output_tokens']} "
            f"| {metrics['avg_total_duration_ms'] / 1000:.2f}s |"
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
            "| Task | Category | Variant | Result | Tools | Calls | Rejects | Duration | Failure |",
            "|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in artifact["rows"]:
        result = "PASS" if row["passed"] else "FAIL"
        lines.append(
            f"| {row['task_id']} | {row['category']} | {row['variant']} | {result} "
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
            "",
        ]
    )
    return "\n".join(lines)


def compare_real_benchmark_artifacts(baseline, candidate):
    """Compare two runs over the exact same benchmark snapshot and task set."""
    baseline = _load_artifact_value(baseline)
    candidate = _load_artifact_value(candidate)
    baseline_snapshot = (baseline.get("benchmark") or {}).get("fixture_snapshot_id")
    candidate_snapshot = (candidate.get("benchmark") or {}).get("fixture_snapshot_id")
    if not baseline_snapshot or baseline_snapshot != candidate_snapshot:
        raise ValueError("benchmark fixture snapshots do not match")
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
                "model_calls_delta": int(after["model_calls"]) - int(before["model_calls"]),
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
        "model": baseline.get("model", ""),
        "fixture_snapshot_id": baseline_snapshot,
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
            "avg_model_calls_delta": _safe_mean(row["model_calls_delta"] for row in task_rows),
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
    rows = [
        row
        for row in artifact.get("rows", [])
        if row.get("variant") == VARIANT_FULL and int(row.get("repetition", 1)) == 1
    ]
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
        f"- Model: `{comparison['model']}`",
        f"- Matched tasks: {comparison['task_count']}",
        f"- Fixture snapshot: `{comparison['fixture_snapshot_id']}`",
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
            "The comparison is accepted only when model, task IDs, and fixture snapshot are identical.",
            "",
        ]
    )
    return "\n".join(lines)
