"""Live multi-turn evidence for Pico working memory and checkpoint recovery.

This is intentionally separate from the single-turn real-world benchmark.  A
continuation episode reconstructs a fresh Pico instance for phase two, so the
only state that crosses the boundary is the persisted session/checkpoint rather
than a provider-side tool conversation.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from pico.agent.actions import ACTION_TOOL
from pico.cli import DEFAULT_OPENAI_MODEL, _load_workspace_env
from pico.run_store import RunStore
from pico.runtime import Pico
from pico.sandbox import DockerSandbox, DockerSandboxConfig, SandboxResult
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext

from . import continuation_benchmark_contract as contract
from . import real_benchmark_evidence as evidence
from .common import git_value, safe_mean, safe_ratio, utc_timestamp
from .real_benchmark import build_real_model_client


class BenchmarkInjectedInterruption(RuntimeError):
    """Expected benchmark fault injected after a durable source-read checkpoint."""


class InjectedInterruptionModelClient:
    """Keep model identity stable while faulting one phase-one continuation.

    The wrapper arms only after Pico has completed and recorded a qualifying
    ``read_file`` action.  Pico creates its checkpoint immediately after the
    action result is recorded, before the next model call receives the injected
    exception.  A phase-two wrapper of the same class has no trigger, avoiding
    an artificial checkpoint-identity mismatch caused by instrumentation.
    """

    def __init__(self, delegate, *, interrupt_after_source_path=""):
        self.delegate = delegate
        self.interrupt_after_source_path = str(interrupt_after_source_path)
        self.armed = False
        self.interruption_delivered = False
        self.last_completion_metadata = {}

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def reset_action_session(self):
        return self.delegate.reset_action_session()

    def complete_action(self, *args, **kwargs):
        if self.armed and not self.interruption_delivered:
            self.interruption_delivered = True
            self.last_completion_metadata = {}
            raise BenchmarkInjectedInterruption(
                "benchmark injected interruption after checkpointed source read"
            )
        action = self.delegate.complete_action(*args, **kwargs)
        self.last_completion_metadata = dict(
            getattr(self.delegate, "last_completion_metadata", {}) or {}
        )
        return action

    def record_action_result(self, action, result):
        outcome = self.delegate.record_action_result(action, result)
        if self.interrupt_after_source_path and _action_reads_path(
            action, self.interrupt_after_source_path
        ):
            self.armed = True
        return outcome


def _action_reads_path(action, source_path):
    if getattr(action, "kind", "") != ACTION_TOOL:
        return False
    if str(getattr(action, "name", "")) != "read_file":
        return False
    args = getattr(action, "args", {}) or {}
    files = args.get("files", []) if isinstance(args, dict) else []
    expected = _normalized_relative(source_path)
    return any(
        isinstance(item, dict)
        and _normalized_relative(item.get("path", "")) == expected
        for item in files
    )


def _normalized_relative(path):
    return Path(str(path)).as_posix().lstrip("./")


def _safe_relative(path, root, *, label):
    raw = str(path or "").strip()
    if not raw:
        raise ValueError(f"{label} must be non-empty")
    candidate = (Path(root) / raw).resolve()
    try:
        return candidate.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes its root: {raw}") from exc


def _validate_tools(value, *, task_id):
    if not isinstance(value, list):
        raise ValueError(f"task {task_id!r} allowed_tools must be a list")
    tools = [str(item).strip() for item in value]
    if not tools or any(not item for item in tools):
        raise ValueError(f"task {task_id!r} allowed_tools must not be empty")
    if len(set(tools)) != len(tools):
        raise ValueError(f"task {task_id!r} allowed_tools must not repeat names")
    missing = {"read_file", "write_file"} - set(tools)
    if missing:
        raise ValueError(
            f"task {task_id!r} must allow read_file and write_file: "
            + ", ".join(sorted(missing))
        )
    return tools


def _validate_prompt(value, *, task_id, field):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"task {task_id!r} {field} must be non-empty")
    return text


def _validate_mutation(value, *, task_id, fixture_root):
    if not isinstance(value, dict):
        raise ValueError(f"task {task_id!r} mutation must be an object")
    mutation = dict(value)
    mutation_type = str(mutation.get("type", "")).strip()
    if mutation_type not in contract.MUTATION_TYPES:
        raise ValueError(
            f"task {task_id!r} mutation type must be one of: "
            + ", ".join(contract.MUTATION_TYPES)
        )
    mutation["type"] = mutation_type
    if mutation_type in {"replace_file", "delete_file", "append_file"}:
        path = _safe_relative(
            mutation.get("path", ""), fixture_root, label="mutation path"
        )
        if not (fixture_root / path).is_file():
            raise ValueError(
                f"task {task_id!r} mutation target does not exist: {path}"
            )
        mutation["path"] = path.as_posix()
    if mutation_type in {"replace_file", "append_file"}:
        content = str(mutation.get("content", ""))
        if not content:
            raise ValueError(f"task {task_id!r} mutation content must be non-empty")
        mutation["content"] = content
    if mutation_type == "set_checkpoint_schema":
        schema_version = str(mutation.get("schema_version", "")).strip()
        if not schema_version:
            raise ValueError(
                f"task {task_id!r} mutation schema_version must be non-empty"
            )
        mutation["schema_version"] = schema_version
    return mutation


def _validate_resume_overrides(value, *, task_id):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"task {task_id!r} resume_overrides must be an object")
    overrides = dict(value)
    unexpected = set(overrides) - {"max_new_tokens", "allowed_tools"}
    if unexpected:
        raise ValueError(
            f"task {task_id!r} has unsupported resume overrides: "
            + ", ".join(sorted(unexpected))
        )
    if "max_new_tokens" in overrides:
        value = int(overrides["max_new_tokens"])
        if value < 1:
            raise ValueError(
                f"task {task_id!r} resume max_new_tokens must be positive"
            )
        overrides["max_new_tokens"] = value
    if "allowed_tools" in overrides:
        overrides["allowed_tools"] = _validate_tools(
            overrides["allowed_tools"], task_id=task_id
        )
    return overrides


def _validate_task(raw_task, *, repo_root, resume):
    required = (
        contract.RESUME_REQUIRED_KEYS if resume else contract.MEMORY_REQUIRED_KEYS
    )
    if not isinstance(raw_task, dict):
        raise ValueError("benchmark task must be an object")
    missing = [key for key in required if key not in raw_task]
    if missing:
        raise ValueError(
            f"task {raw_task.get('id', '<unknown>')!r} missing: "
            + ", ".join(missing)
        )
    task = dict(raw_task)
    task_id = str(task["id"] or "").strip()
    if not task_id:
        raise ValueError("task id must be non-empty")
    task["id"] = task_id
    task["category"] = str(task["category"] or "").strip()
    if not task["category"]:
        raise ValueError(f"task {task_id!r} category must be non-empty")
    fixture_relative = _safe_relative(
        task["fixture_repo"], repo_root, label="fixture repo"
    )
    fixture_root = repo_root / fixture_relative
    if not fixture_root.is_dir():
        raise ValueError(f"task {task_id!r} fixture repo does not exist")
    task["fixture_repo"] = fixture_relative.as_posix()
    source_path = _safe_relative(
        task["source_path"], fixture_root, label="source path"
    )
    if not (fixture_root / source_path).is_file():
        raise ValueError(f"task {task_id!r} source path does not exist: {source_path}")
    task["source_path"] = source_path.as_posix()
    task["phase_one_prompt"] = _validate_prompt(
        task["phase_one_prompt"], task_id=task_id, field="phase_one_prompt"
    )
    task["phase_two_prompt"] = _validate_prompt(
        task["phase_two_prompt"], task_id=task_id, field="phase_two_prompt"
    )
    task["allowed_tools"] = _validate_tools(task["allowed_tools"], task_id=task_id)
    task["step_budget"] = int(task["step_budget"])
    if task["step_budget"] < 1:
        raise ValueError(f"task {task_id!r} step_budget must be positive")
    task["expected_output"] = str(task["expected_output"] or "")
    if not task["expected_output"] or "\n" in task["expected_output"]:
        raise ValueError(
            f"task {task_id!r} expected_output must be one non-empty line"
        )

    if not resume:
        return task

    expected_status = str(task["expected_resume_status"] or "").strip()
    if expected_status not in contract.RESUME_STATUSES:
        raise ValueError(
            f"task {task_id!r} expected_resume_status must be one of: "
            + ", ".join(contract.RESUME_STATUSES)
        )
    task["expected_resume_status"] = expected_status
    stale_paths = [
        _safe_relative(path, fixture_root, label="expected stale path").as_posix()
        for path in task["expected_stale_paths"]
    ]
    if len(set(stale_paths)) != len(stale_paths):
        raise ValueError(f"task {task_id!r} expected_stale_paths must not repeat")
    task["expected_stale_paths"] = sorted(stale_paths)
    mismatch_fields = [
        str(item).strip() for item in task["expected_runtime_identity_mismatch_fields"]
    ]
    if any(not item for item in mismatch_fields):
        raise ValueError(
            f"task {task_id!r} expected runtime mismatch fields must be non-empty"
        )
    task["expected_runtime_identity_mismatch_fields"] = sorted(set(mismatch_fields))
    read_requirement = str(task["phase_two_source_read_requirement"] or "").strip()
    if read_requirement not in contract.SOURCE_READ_REQUIREMENTS:
        raise ValueError(
            f"task {task_id!r} phase_two_source_read_requirement must be one of: "
            + ", ".join(contract.SOURCE_READ_REQUIREMENTS)
        )
    task["phase_two_source_read_requirement"] = read_requirement
    task["mutation"] = _validate_mutation(
        task["mutation"], task_id=task_id, fixture_root=fixture_root
    )
    task["resume_overrides"] = _validate_resume_overrides(
        task.get("resume_overrides"), task_id=task_id
    )
    return task


def validate_continuation_benchmark(payload, repo_root):
    """Validate and normalize the frozen continuation benchmark manifest."""
    if not isinstance(payload, dict):
        raise ValueError("continuation benchmark must be an object")
    if int(payload.get("schema_version", 0)) != contract.CONTINUATION_BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported continuation benchmark schema_version")
    repo_root = Path(repo_root).resolve()
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("continuation benchmark name must be non-empty")
    memory_cases = payload.get("memory_cases")
    resume_cases = payload.get("resume_cases")
    if not isinstance(memory_cases, list) or not memory_cases:
        raise ValueError("memory_cases must be a non-empty list")
    if not isinstance(resume_cases, list) or not resume_cases:
        raise ValueError("resume_cases must be a non-empty list")
    normalized_memory = [
        _validate_task(task, repo_root=repo_root, resume=False)
        for task in memory_cases
    ]
    normalized_resume = [
        _validate_task(task, repo_root=repo_root, resume=True)
        for task in resume_cases
    ]
    all_ids = [task["id"] for task in [*normalized_memory, *normalized_resume]]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("continuation benchmark task ids must be globally unique")
    verifier = repo_root / contract.HIDDEN_VERIFIER_SOURCE
    if not verifier.is_file():
        raise ValueError("continuation hidden verifier source does not exist")
    return {
        **dict(payload),
        "name": name,
        "memory_cases": normalized_memory,
        "resume_cases": normalized_resume,
    }


def load_continuation_benchmark(
    path=contract.DEFAULT_CONTINUATION_BENCHMARK_PATH, repo_root=None
):
    path = Path(path).resolve()
    root = Path(repo_root).resolve() if repo_root else path.parent.parent
    return validate_continuation_benchmark(
        json.loads(path.read_text(encoding="utf-8")), root
    )


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
            digest.update(str(path.relative_to(fixture_root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _evaluation_snapshot_id(benchmark, tasks, repo_root):
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "schema_version": benchmark["schema_version"],
                "memory_cases": sorted(
                    (dict(task) for task in tasks if "expected_resume_status" not in task),
                    key=lambda task: task["id"],
                ),
                "resume_cases": sorted(
                    (dict(task) for task in tasks if "expected_resume_status" in task),
                    key=lambda task: task["id"],
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(b"\0fixtures\0")
    digest.update(_fixture_snapshot_id(tasks, repo_root).encode("ascii"))
    digest.update(b"\0verifier\0")
    digest.update((Path(repo_root) / contract.HIDDEN_VERIFIER_SOURCE).read_bytes())
    return "sha256:" + digest.hexdigest()


def _load_benchmark_env(repo_root, env_file):
    if env_file is None:
        return _load_workspace_env(repo_root)
    path = Path(env_file).expanduser().resolve()
    if path.name != ".env.local":
        raise ValueError("env_file must point to a .env.local file")
    if not path.is_file():
        raise ValueError("env_file does not exist")
    return _load_workspace_env(path.parent)


def _phase_read_metrics(report, source_path):
    attempted = 0
    successful = 0
    expected = _normalized_relative(source_path)
    for entry in report.get("tool_audit") or []:
        if str(entry.get("name", "")) != "read_file":
            continue
        matches = sum(
            _normalized_relative(path) == expected
            for path in entry.get("paths") or []
        )
        attempted += matches
        if str(entry.get("status", "")) == "ok":
            successful += matches
    return {
        "attempted_physical_file_accesses": attempted,
        "successful_physical_file_accesses": successful,
    }


def _memory_summary_present(agent, source_path):
    summaries = agent.memory.to_dict().get("file_summaries", {})
    return _normalized_relative(source_path) in summaries


def _public_phase(phase, *, include_final_answer=False):
    keys = (
        "run_id",
        "status",
        "stop_reason",
        "tool_steps",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "model_duration_ms",
        "model_failures",
        "model_action_rejections",
        "action_protocols",
        "source_read",
        "workspace_changed",
        "trace_parse_errors",
        "recent_run_selection_count",
        "memory_summary_present_before_phase",
        "resume_status",
    )
    public = {key: phase[key] for key in keys}
    if include_final_answer:
        public["final_answer"] = phase["final_answer"]
    return public


def _phase_execution(agent, prompt, source_path):
    run_store = agent.run_store
    existing = {
        path.name for path in run_store.root.glob("run_*") if path.is_dir()
    }
    memory_summary_present = _memory_summary_present(agent, source_path)
    resume_status = dict(agent.resume_state)
    started = time.monotonic()
    final_answer = agent.ask(prompt)
    agent_duration_ms = int((time.monotonic() - started) * 1000)
    task_state = agent.current_task_state
    report = run_store.load_report(task_state.run_id)
    new_run_dirs = sorted(
        [
            path
            for path in run_store.root.glob("run_*")
            if path.is_dir() and path.name not in existing
        ],
        key=lambda path: path.name,
    )
    attempt_trace = evidence._attempt_trace_metrics(
        run_store.run_dir(task_state.run_id), new_run_dirs, agent.root
    )
    trace = attempt_trace["total"]
    prompt_metadata = dict(report.get("prompt_metadata") or {})
    recent_runs = dict(prompt_metadata.get("recent_runs") or {})
    return {
        "run_id": task_state.run_id,
        "status": str(task_state.status),
        "stop_reason": str(task_state.stop_reason),
        "final_answer": str(final_answer),
        "tool_steps": int(task_state.tool_steps),
        "model_calls": int(trace["model_calls"]),
        "input_tokens": int(trace["input_tokens"]),
        "output_tokens": int(trace["output_tokens"]),
        "cached_tokens": int(trace["cached_tokens"]),
        "model_duration_ms": int(trace["model_duration_ms"]),
        "model_failures": int(trace["model_failures"]),
        "model_action_rejections": int(trace["model_action_rejections"]),
        "action_protocols": list(trace["action_protocols"]),
        "source_read": _phase_read_metrics(report, source_path),
        "workspace_changed": any(
            bool(item.get("workspace_changed"))
            for item in report.get("tool_audit") or []
        ),
        "trace_parse_errors": list(trace["trace_parse_errors"]),
        "recent_run_selection_count": int(recent_runs.get("selected_count") or 0),
        "memory_summary_present_before_phase": bool(memory_summary_present),
        "resume_status": dict(resume_status),
        "agent_duration_ms": agent_duration_ms,
        "report": report,
        "run_dirs": new_run_dirs,
    }


def _source_read_requirement_met(phase, requirement):
    source_read = phase["source_read"]
    if requirement == "none":
        return True
    if requirement == "attempted":
        return source_read["attempted_physical_file_accesses"] >= 1
    if requirement == "successful":
        return source_read["successful_physical_file_accesses"] >= 1
    raise ValueError(f"unsupported source read requirement: {requirement}")


def _phase_totals(*phases):
    return {
        "tool_steps": sum(int(phase["tool_steps"]) for phase in phases),
        "model_calls": sum(int(phase["model_calls"]) for phase in phases),
        "input_tokens": sum(int(phase["input_tokens"]) for phase in phases),
        "output_tokens": sum(int(phase["output_tokens"]) for phase in phases),
        "cached_tokens": sum(int(phase["cached_tokens"]) for phase in phases),
        "model_duration_ms": sum(
            int(phase["model_duration_ms"]) for phase in phases
        ),
        "raw_model_failures": sum(
            int(phase["model_failures"]) for phase in phases
        ),
        "model_action_rejections": sum(
            int(phase["model_action_rejections"]) for phase in phases
        ),
        "agent_duration_ms": sum(
            int(phase["agent_duration_ms"]) for phase in phases
        ),
    }


def _hash_path(path):
    path = Path(path)
    if not path.exists() or not path.is_file():
        return "missing"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _apply_mutation(task, workspace_root, session_store, session_id):
    mutation = dict(task["mutation"])
    mutation_type = mutation["type"]
    record = {"type": mutation_type, "paths": [], "before_after_sha256": {}}
    if mutation_type == "none":
        return record
    if mutation_type in {"replace_file", "delete_file", "append_file"}:
        path = Path(workspace_root) / mutation["path"]
        before = _hash_path(path)
        if mutation_type == "replace_file":
            path.write_text(mutation["content"], encoding="utf-8")
        elif mutation_type == "append_file":
            with path.open("a", encoding="utf-8") as handle:
                handle.write(mutation["content"])
        else:
            path.unlink()
        record["paths"] = [mutation["path"]]
        record["before_after_sha256"] = {
            mutation["path"]: {"before": before, "after": _hash_path(path)}
        }
        return record
    if mutation_type == "set_checkpoint_schema":
        session = session_store.load(session_id)
        checkpoint_id = str(session["checkpoints"].get("current_id", ""))
        checkpoint = session["checkpoints"].get("items", {}).get(checkpoint_id)
        if not isinstance(checkpoint, dict):
            raise RuntimeError("cannot mutate missing checkpoint schema")
        before = str(checkpoint.get("schema_version", ""))
        checkpoint["schema_version"] = mutation["schema_version"]
        session_store.save(session)
        record["checkpoint_schema"] = {
            "before": before,
            "after": mutation["schema_version"],
        }
        return record
    raise ValueError(f"unsupported mutation type: {mutation_type}")


def _failure_reasons(*, conditions):
    return sorted(name for name, ok in conditions.items() if not ok)


def _memory_variant_flags(variant):
    if variant not in contract.MEMORY_VARIANTS:
        raise ValueError(f"unsupported memory benchmark variant: {variant}")
    return {
        "memory": variant == contract.VARIANT_WORKING_MEMORY,
        "read_only_dedup": True,
        "repo_map": False,
        "context_reduction": False,
        "prompt_cache": False,
        "dynamic_budget": False,
    }


def _resume_flags():
    return _memory_variant_flags(contract.VARIANT_WORKING_MEMORY)


@dataclass
class LiveContinuationBenchmarkRunner:
    benchmark_path: Path | str = contract.DEFAULT_CONTINUATION_BENCHMARK_PATH
    artifact_path: Path | str = contract.DEFAULT_CONTINUATION_ARTIFACT_PATH
    report_path: Path | str = contract.DEFAULT_CONTINUATION_REPORT_PATH
    workspace_root: Path | str = contract.DEFAULT_CONTINUATION_WORKSPACE_ROOT
    provider: str = "openai"
    model: str | None = None
    base_url: str | None = None
    env_file: Path | str | None = None
    repetitions: int = 3
    max_new_tokens: int = 512
    verifier_timeout: int = 90
    require_clean_worktree: bool = False
    sandbox_config: DockerSandboxConfig | None = None
    model_client_factory: object | None = None

    def __post_init__(self):
        self.benchmark_path = Path(self.benchmark_path).resolve()
        self.repo_root = self.benchmark_path.parent.parent
        self.artifact_path = Path(self.artifact_path)
        self.report_path = Path(self.report_path)
        self.workspace_root = Path(self.workspace_root)
        if str(self.provider).lower() != "openai":
            raise ValueError("provider must be 'openai'")
        self.provider = "openai"
        if int(self.repetitions) < 1:
            raise ValueError("repetitions must be positive")
        if int(self.max_new_tokens) < 1:
            raise ValueError("max_new_tokens must be positive")
        self._workspace_env = _load_benchmark_env(self.repo_root, self.env_file)
        self.model = str(
            self.model
            or self._workspace_env.get("OPENAI_MODEL")
            or DEFAULT_OPENAI_MODEL
        ).strip()
        self.reasoning_effort = (
            self._workspace_env.get("OPENAI_REASONING_EFFORT", "").strip() or None
        )

    def run(self, task_ids=None):
        benchmark = load_continuation_benchmark(self.benchmark_path, self.repo_root)
        selected_ids = {str(task_id) for task_id in (task_ids or ())}
        memory_cases = [
            task
            for task in benchmark["memory_cases"]
            if not selected_ids or task["id"] in selected_ids
        ]
        resume_cases = [
            task
            for task in benchmark["resume_cases"]
            if not selected_ids or task["id"] in selected_ids
        ]
        all_selected = [*memory_cases, *resume_cases]
        unknown_ids = selected_ids - {task["id"] for task in all_selected}
        if unknown_ids:
            raise ValueError(
                "unknown continuation benchmark task ids: "
                + ", ".join(sorted(unknown_ids))
            )
        if not all_selected:
            raise ValueError("at least one continuation task must be selected")
        self._preflight()
        rows = []
        for repetition in range(1, int(self.repetitions) + 1):
            for index, task in enumerate(memory_cases):
                variants = list(contract.MEMORY_VARIANTS)
                if (repetition + index) % 2:
                    variants.reverse()
                for position, variant in enumerate(variants, start=1):
                    rows.append(
                        self.run_memory_case(
                            task,
                            variant=variant,
                            repetition=repetition,
                            scheduled_variant_position=position,
                        )
                    )
            for task in resume_cases:
                rows.append(self.run_resume_case(task, repetition=repetition))
        all_tasks = [*memory_cases, *resume_cases]
        git_status = git_value(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            fallback=None,
            preserve_empty=True,
        )
        artifact = {
            "schema_version": contract.CONTINUATION_ARTIFACT_SCHEMA_VERSION,
            "artifact_type": "pico-live-continuation-benchmark",
            "execution_mode": "live_llm_multiturn",
            "captured_at": utc_timestamp(),
            "runtime": {
                "commit_sha": git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": git_value(["branch", "--show-current"], cwd=self.repo_root),
                "working_tree_dirty": None if git_status is None else bool(git_status),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
            },
            "benchmark": {
                "name": benchmark["name"],
                "description": str(benchmark.get("description", "")),
                "source": str(self.benchmark_path.relative_to(self.repo_root)),
                "memory_task_count": len(memory_cases),
                "resume_scenario_count": len(resume_cases),
                "task_count": len(all_tasks),
                "task_ids": [task["id"] for task in all_tasks],
                "fixture_snapshot_id": _fixture_snapshot_id(all_tasks, self.repo_root),
                "evaluation_snapshot_id": _evaluation_snapshot_id(
                    benchmark, all_tasks, self.repo_root
                ),
            },
            "provider": self.provider,
            "model": self.model,
            "repetitions": int(self.repetitions),
            "run_config": {
                "temperature": 0.0,
                "reasoning_effort": self.reasoning_effort,
                "max_new_tokens": int(self.max_new_tokens),
                "verifier_timeout_seconds": int(self.verifier_timeout),
                "require_clean_worktree": bool(self.require_clean_worktree),
                "phase_two_model_client": "fresh client and fresh Pico instance",
                "memory_variant_flags": {
                    variant: _memory_variant_flags(variant)
                    for variant in contract.MEMORY_VARIANTS
                },
                "resume_feature_flags": _resume_flags(),
                "expected_interruption": (
                    "injected only after a completed qualifying read_file result; "
                    "the following Pico checkpoint is durable before the next model call"
                ),
            },
            "sandbox": (self.sandbox_config or DockerSandboxConfig()).__dict__,
            "summary": summarize_continuation_rows(rows),
            "rows": rows,
        }
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            render_continuation_markdown(artifact), encoding="utf-8"
        )
        return artifact

    def run_memory_case(
        self, task, *, variant, repetition, scheduled_variant_position=1
    ):
        workspace_root, workspace_label = self._prepare_workspace(
            task,
            repetition=repetition,
            family="memory",
            variant=variant,
        )
        run_store = RunStore(workspace_root / ".pico" / "runs")
        session_store = SessionStore(workspace_root / ".pico" / "sessions")
        feature_flags = _memory_variant_flags(variant)
        phase_one_agent = self._new_agent(
            model_client=self._new_model_client(),
            workspace_root=workspace_root,
            session_store=session_store,
            run_store=run_store,
            task=task,
            feature_flags=feature_flags,
        )
        phase_one = _phase_execution(
            phase_one_agent, task["phase_one_prompt"], task["source_path"]
        )
        phase_two_agent = self._resume_agent(
            model_client=self._new_model_client(),
            workspace_root=workspace_root,
            session_store=session_store,
            run_store=run_store,
            session_id=phase_one_agent.session["id"],
            task=task,
            feature_flags=feature_flags,
        )
        phase_two = _phase_execution(
            phase_two_agent, task["phase_two_prompt"], task["source_path"]
        )
        all_run_dirs = [*phase_one["run_dirs"], *phase_two["run_dirs"]]
        workspace_isolation = self._workspace_isolation(
            workspace_root, all_run_dirs, workspace_label
        )
        verifier_result, verifier_duration_ms, verifier_skipped = self._verify(
            workspace_root,
            expected_output=task["expected_output"],
            output_path="followup-result.txt",
            enabled=workspace_isolation["ok"],
        )
        conditions = {
            "phase_one_completed": phase_one["status"] == "completed",
            "phase_one_read_source": phase_one["source_read"][
                "successful_physical_file_accesses"
            ] >= 1,
            "phase_one_no_workspace_change": not phase_one["workspace_changed"],
            "phase_one_ack_without_fact": phase_one["final_answer"].strip() == "ACK",
            "phase_two_completed": phase_two["status"] == "completed",
            "phase_two_full_valid_resume": phase_two["resume_status"].get("status")
            == "full-valid",
            "phase_two_no_recent_run_channel": phase_two["recent_run_selection_count"] == 0,
            "memory_state_matches_variant": phase_two[
                "memory_summary_present_before_phase"
            ]
            == (variant == contract.VARIANT_WORKING_MEMORY),
            "workspace_isolation": workspace_isolation["ok"],
            "hidden_verifier": verifier_result.returncode == 0,
            "trace_parses": not phase_one["trace_parse_errors"]
            and not phase_two["trace_parse_errors"],
        }
        totals = _phase_totals(phase_one, phase_two)
        totals["verifier_duration_ms"] = verifier_duration_ms
        totals["total_duration_ms"] = (
            totals["agent_duration_ms"] + verifier_duration_ms
        )
        return {
            "episode_type": "memory_followup",
            "task_id": task["id"],
            "category": task["category"],
            "variant": variant,
            "repetition": int(repetition),
            "scheduled_variant_position": int(scheduled_variant_position),
            "passed": all(conditions.values()),
            "failure_reasons": _failure_reasons(conditions=conditions),
            "phase_one": _public_phase(phase_one, include_final_answer=True),
            "phase_two": _public_phase(phase_two),
            "followup_source_read": dict(phase_two["source_read"]),
            "workspace_isolation": workspace_isolation,
            "verifier": {
                "skipped": verifier_skipped,
                "exit_code": int(verifier_result.returncode),
                "timed_out": bool(verifier_result.timed_out),
                "stdout": verifier_result.stdout[-1000:],
                "stderr": verifier_result.stderr[-1000:],
            },
            "totals": totals,
        }

    def run_resume_case(self, task, *, repetition):
        workspace_root, workspace_label = self._prepare_workspace(
            task,
            repetition=repetition,
            family="resume",
            variant="checkpoint_resume",
        )
        run_store = RunStore(workspace_root / ".pico" / "runs")
        session_store = SessionStore(workspace_root / ".pico" / "sessions")
        feature_flags = _resume_flags()
        phase_one_client = InjectedInterruptionModelClient(
            self._new_model_client(),
            interrupt_after_source_path=task["source_path"],
        )
        phase_one_agent = self._new_agent(
            model_client=phase_one_client,
            workspace_root=workspace_root,
            session_store=session_store,
            run_store=run_store,
            task=task,
            feature_flags=feature_flags,
        )
        phase_one = _phase_execution(
            phase_one_agent, task["phase_one_prompt"], task["source_path"]
        )
        session_id = phase_one_agent.session["id"]
        mutation = _apply_mutation(task, workspace_root, session_store, session_id)
        overrides = dict(task.get("resume_overrides") or {})
        phase_two_task = {
            **task,
            "allowed_tools": list(overrides.get("allowed_tools", task["allowed_tools"])),
        }
        phase_two_max_new_tokens = int(
            overrides.get("max_new_tokens", self.max_new_tokens)
        )
        phase_two_client = InjectedInterruptionModelClient(self._new_model_client())
        phase_two_agent = self._resume_agent(
            model_client=phase_two_client,
            workspace_root=workspace_root,
            session_store=session_store,
            run_store=run_store,
            session_id=session_id,
            task=phase_two_task,
            feature_flags=feature_flags,
            max_new_tokens=phase_two_max_new_tokens,
        )
        phase_two = _phase_execution(
            phase_two_agent, task["phase_two_prompt"], task["source_path"]
        )
        all_run_dirs = [*phase_one["run_dirs"], *phase_two["run_dirs"]]
        workspace_isolation = self._workspace_isolation(
            workspace_root, all_run_dirs, workspace_label
        )
        verifier_result, verifier_duration_ms, verifier_skipped = self._verify(
            workspace_root,
            expected_output=task["expected_output"],
            output_path="recovery-result.txt",
            enabled=workspace_isolation["ok"],
        )
        actual_resume_state = phase_two["resume_status"]
        expected_stale_paths = list(task["expected_stale_paths"])
        expected_mismatch_fields = list(
            task["expected_runtime_identity_mismatch_fields"]
        )
        conditions = {
            "injected_interruption_delivered": phase_one_client.interruption_delivered,
            "phase_one_stopped_by_injected_model_error": phase_one["status"]
            == "failed"
            and phase_one["stop_reason"] == "model_error",
            "phase_one_read_source": phase_one["source_read"][
                "successful_physical_file_accesses"
            ] >= 1,
            "phase_one_no_workspace_change": not phase_one["workspace_changed"],
            "phase_two_completed": phase_two["status"] == "completed",
            "resume_status_matches": actual_resume_state.get("status")
            == task["expected_resume_status"],
            "stale_paths_match": sorted(actual_resume_state.get("stale_paths") or [])
            == expected_stale_paths,
            "runtime_identity_mismatch_fields_match": sorted(
                actual_resume_state.get("runtime_identity_mismatch_fields") or []
            )
            == expected_mismatch_fields,
            "phase_two_source_read_requirement": _source_read_requirement_met(
                phase_two, task["phase_two_source_read_requirement"]
            ),
            "phase_two_no_recent_run_channel": phase_two["recent_run_selection_count"] == 0,
            "workspace_isolation": workspace_isolation["ok"],
            "hidden_verifier": verifier_result.returncode == 0,
            "trace_parses": not phase_one["trace_parse_errors"]
            and not phase_two["trace_parse_errors"],
        }
        totals = _phase_totals(phase_one, phase_two)
        expected_injected_model_failures = 1 if phase_one_client.interruption_delivered else 0
        totals["expected_injected_model_failures"] = expected_injected_model_failures
        totals["unexpected_model_failures"] = max(
            0, totals["raw_model_failures"] - expected_injected_model_failures
        )
        totals["verifier_duration_ms"] = verifier_duration_ms
        totals["total_duration_ms"] = (
            totals["agent_duration_ms"] + verifier_duration_ms
        )
        return {
            "episode_type": "checkpoint_resume",
            "task_id": task["id"],
            "category": task["category"],
            "variant": "checkpoint_resume",
            "repetition": int(repetition),
            "passed": all(conditions.values()),
            "failure_reasons": _failure_reasons(conditions=conditions),
            "expected_resume_status": task["expected_resume_status"],
            "expected_stale_paths": expected_stale_paths,
            "expected_runtime_identity_mismatch_fields": expected_mismatch_fields,
            "phase_two_source_read_requirement": task[
                "phase_two_source_read_requirement"
            ],
            "mutation": mutation,
            "phase_one": _public_phase(phase_one),
            "phase_two": _public_phase(phase_two),
            "phase_two_source_read": dict(phase_two["source_read"]),
            "workspace_isolation": workspace_isolation,
            "verifier": {
                "skipped": verifier_skipped,
                "exit_code": int(verifier_result.returncode),
                "timed_out": bool(verifier_result.timed_out),
                "stdout": verifier_result.stdout[-1000:],
                "stderr": verifier_result.stderr[-1000:],
            },
            "totals": totals,
        }

    def _prepare_workspace(self, task, *, repetition, family, variant):
        fixture_source = (self.repo_root / task["fixture_repo"]).resolve()
        relative_workspace = (
            Path(f"rep-{int(repetition)}") / family / variant / task["id"] / fixture_source.name
        )
        workspace_root = (self.workspace_root / relative_workspace).resolve()
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        workspace_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, workspace_root)
        return workspace_root, relative_workspace.as_posix()

    def _new_model_client(self):
        if self.model_client_factory is not None:
            return self.model_client_factory()
        return build_real_model_client(
            self.model,
            self.base_url,
            env=self._workspace_env,
        )

    def _new_agent(
        self,
        *,
        model_client,
        workspace_root,
        session_store,
        run_store,
        task,
        feature_flags,
        max_new_tokens=None,
    ):
        return Pico(
            model_client=model_client,
            workspace=WorkspaceContext.build(
                workspace_root, repo_root_override=workspace_root
            ),
            session_store=session_store,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=int(max_new_tokens or self.max_new_tokens),
            allowed_tools=tuple(task["allowed_tools"]),
            feature_flags=feature_flags,
            sandbox=self._sandbox(workspace_root),
        )

    def _resume_agent(
        self,
        *,
        model_client,
        workspace_root,
        session_store,
        run_store,
        session_id,
        task,
        feature_flags,
        max_new_tokens=None,
    ):
        return Pico.from_session(
            model_client,
            WorkspaceContext.build(workspace_root, repo_root_override=workspace_root),
            session_store,
            session_id,
            run_store=run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=int(max_new_tokens or self.max_new_tokens),
            allowed_tools=tuple(task["allowed_tools"]),
            feature_flags=feature_flags,
            sandbox=self._sandbox(workspace_root),
        )

    def _workspace_isolation(self, workspace_root, run_dirs, workspace_label):
        audit = evidence._workspace_isolation_audit(
            workspace_root,
            run_dirs,
            {
                "verifier_files": [
                    {
                        "source": contract.HIDDEN_VERIFIER_SOURCE.as_posix(),
                        "target": contract.HIDDEN_VERIFIER_TARGET.as_posix(),
                    }
                ]
            },
        )
        return evidence.public_workspace_isolation_audit(
            audit, workspace_root, workspace_label
        )

    def _verify(self, workspace_root, *, expected_output, output_path, enabled):
        if not enabled:
            return (
                SandboxResult(
                    returncode=125,
                    stderr="verifier skipped: workspace isolation audit failed",
                ),
                0,
                True,
            )
        source = self.repo_root / contract.HIDDEN_VERIFIER_SOURCE
        target = Path(workspace_root) / contract.HIDDEN_VERIFIER_TARGET
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        command = " ".join(
            [
                "python",
                shlex.quote(contract.HIDDEN_VERIFIER_TARGET.as_posix()),
                "--output-path",
                shlex.quote(str(output_path)),
                "--expected-output",
                shlex.quote(str(expected_output)),
            ]
        )
        started = time.monotonic()
        try:
            result = self._sandbox(workspace_root).run(
                command,
                cwd=workspace_root,
                timeout=int(self.verifier_timeout),
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
        finally:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()
            except OSError:
                pass
        return result, int((time.monotonic() - started) * 1000), False

    def _sandbox(self, workspace_root):
        return DockerSandbox(
            workspace_root,
            config=self.sandbox_config or DockerSandboxConfig(),
        )

    def _preflight(self):
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        if self.require_clean_worktree:
            git_status = git_value(
                ["status", "--porcelain", "--untracked-files=all"],
                cwd=self.repo_root,
                fallback=None,
                preserve_empty=True,
            )
            if git_status is None:
                raise RuntimeError("cannot verify that the benchmark worktree is clean")
            if git_status:
                raise RuntimeError("benchmark requires a clean git worktree")
        if self.model_client_factory is None:
            # Fail before copying fixtures or making any provider request when
            # configuration is incomplete. The actual episode still receives a
            # fresh client for each phase.
            self._new_model_client()
        DockerSandbox(
            self.workspace_root,
            config=self.sandbox_config or DockerSandboxConfig(),
        ).ensure_ready()


def summarize_continuation_rows(rows):
    rows = list(rows)
    memory_rows = [row for row in rows if row["episode_type"] == "memory_followup"]
    resume_rows = [row for row in rows if row["episode_type"] == "checkpoint_resume"]
    memory_variants = {}
    for variant in contract.MEMORY_VARIANTS:
        selected = [row for row in memory_rows if row["variant"] == variant]
        if not selected:
            continue
        memory_variants[variant] = {
            "attempt_count": len(selected),
            "passed": sum(bool(row["passed"]) for row in selected),
            "pass_rate": safe_ratio(
                sum(bool(row["passed"]) for row in selected), len(selected)
            ),
            "hidden_verifier_passed": sum(
                row["verifier"]["exit_code"] == 0 for row in selected
            ),
            "phase_two_source_read_attempted": sum(
                row["followup_source_read"]["attempted_physical_file_accesses"]
                for row in selected
            ),
            "phase_two_source_read_successful": sum(
                row["followup_source_read"]["successful_physical_file_accesses"]
                for row in selected
            ),
            "verified_read_free_followups": sum(
                bool(row["passed"])
                and row["followup_source_read"]["successful_physical_file_accesses"]
                == 0
                for row in selected
            ),
            "avg_model_calls": safe_mean(
                row["totals"]["model_calls"] for row in selected
            ),
            "total_input_tokens": sum(
                row["totals"]["input_tokens"] for row in selected
            ),
            "total_output_tokens": sum(
                row["totals"]["output_tokens"] for row in selected
            ),
            "avg_duration_ms": safe_mean(
                row["totals"]["total_duration_ms"] for row in selected
            ),
        }
    pair_map = {}
    for row in memory_rows:
        key = (row["task_id"], int(row["repetition"]))
        pair_map.setdefault(key, {})[row["variant"]] = row
    pairs = [
        variants
        for variants in pair_map.values()
        if set(variants) == set(contract.MEMORY_VARIANTS)
    ]
    paired_read_comparison = {
        "complete_pair_count": len(pairs),
        "both_passed_pair_count": sum(
            all(row["passed"] for row in pair.values()) for pair in pairs
        ),
        "working_memory_phase_two_successful_source_reads": sum(
            pair[contract.VARIANT_WORKING_MEMORY]["followup_source_read"][
                "successful_physical_file_accesses"
            ]
            for pair in pairs
        ),
        "memory_disabled_phase_two_successful_source_reads": sum(
            pair[contract.VARIANT_MEMORY_DISABLED]["followup_source_read"][
                "successful_physical_file_accesses"
            ]
            for pair in pairs
        ),
        "pairs_with_fewer_candidate_reads": sum(
            pair[contract.VARIANT_WORKING_MEMORY]["followup_source_read"][
                "successful_physical_file_accesses"
            ]
            < pair[contract.VARIANT_MEMORY_DISABLED]["followup_source_read"][
                "successful_physical_file_accesses"
            ]
            for pair in pairs
        ),
    }
    paired_read_comparison["successful_source_read_delta"] = (
        paired_read_comparison["working_memory_phase_two_successful_source_reads"]
        - paired_read_comparison["memory_disabled_phase_two_successful_source_reads"]
    )

    resume_tasks = {}
    for task_id in sorted({row["task_id"] for row in resume_rows}):
        selected = [row for row in resume_rows if row["task_id"] == task_id]
        resume_tasks[task_id] = {
            "attempt_count": len(selected),
            "passed": sum(bool(row["passed"]) for row in selected),
            "pass_rate": safe_ratio(
                sum(bool(row["passed"]) for row in selected), len(selected)
            ),
            "status_matches": sum(
                row["phase_two"]["resume_status"].get("status")
                == row["expected_resume_status"]
                for row in selected
            ),
            "hidden_verifier_passed": sum(
                row["verifier"]["exit_code"] == 0 for row in selected
            ),
            "phase_two_source_read_successful": sum(
                row["phase_two_source_read"]["successful_physical_file_accesses"]
                for row in selected
            ),
        }
    detected_drift = [
        row
        for row in resume_rows
        if row["expected_resume_status"] in {"partial-stale", "workspace-mismatch"}
    ]
    resume_summary = {
        "attempt_count": len(resume_rows),
        "passed": sum(bool(row["passed"]) for row in resume_rows),
        "pass_rate": safe_ratio(
            sum(bool(row["passed"]) for row in resume_rows), len(resume_rows)
        ),
        "injected_interruptions_delivered": sum(
            row["phase_one"]["status"] == "failed"
            and row["phase_one"]["stop_reason"] == "model_error"
            for row in resume_rows
        ),
        "status_matches": sum(
            row["phase_two"]["resume_status"].get("status")
            == row["expected_resume_status"]
            for row in resume_rows
        ),
        "safe_task_recoveries": sum(
            bool(row["passed"]) for row in resume_rows
        ),
        "hidden_verifier_passed": sum(
            row["verifier"]["exit_code"] == 0 for row in resume_rows
        ),
        "drift_detection_attempt_count": len(detected_drift),
        "drift_status_matches": sum(
            row["phase_two"]["resume_status"].get("status")
            == row["expected_resume_status"]
            for row in detected_drift
        ),
        "unexpected_model_failures": sum(
            row["totals"]["unexpected_model_failures"] for row in resume_rows
        ),
        "total_input_tokens": sum(
            row["totals"]["input_tokens"] for row in resume_rows
        ),
        "total_output_tokens": sum(
            row["totals"]["output_tokens"] for row in resume_rows
        ),
        "tasks": resume_tasks,
    }
    return {
        "episode_count": len(rows),
        "passed": sum(bool(row["passed"]) for row in rows),
        "pass_rate": safe_ratio(sum(bool(row["passed"]) for row in rows), len(rows)),
        "workspace_isolation_failures": sum(
            not row["workspace_isolation"]["ok"] for row in rows
        ),
        "trace_parse_error_count": sum(
            len(row["phase_one"]["trace_parse_errors"])
            + len(row["phase_two"]["trace_parse_errors"])
            for row in rows
        ),
        "memory": {
            "variants": memory_variants,
            "paired_read_comparison": paired_read_comparison,
        },
        "resume": resume_summary,
    }


def render_continuation_markdown(artifact):
    summary = artifact["summary"]
    memory = summary["memory"]
    resume = summary["resume"]
    pairs = memory["paired_read_comparison"]
    lines = [
        f"# {artifact['benchmark']['name']}",
        "",
        "## Result",
        "",
        (
            f"- Overall: **{summary['passed']}/{summary['episode_count']} "
            f"({summary['pass_rate']:.1%})** episodes met every pre-registered "
            "runtime, isolation, and hidden-verifier criterion."
        ),
        (
            "- Memory paired source reads (working-memory − memory-disabled): "
            f"**{pairs['successful_source_read_delta']}** across "
            f"{pairs['complete_pair_count']} complete pairs."
        ),
        (
            f"- Checkpoint recovery: **{resume['passed']}/{resume['attempt_count']} "
            f"({resume['pass_rate']:.1%})** safe task recoveries; status "
            f"matches **{resume['status_matches']}/{resume['attempt_count']}**."
        ),
        (
            "- Drift-status matches: "
            f"**{resume['drift_status_matches']}/{resume['drift_detection_attempt_count']}**; "
            f"unexpected model failures: **{resume['unexpected_model_failures']}**."
        ),
        "",
        "## Memory follow-up comparison",
        "",
        "| Variant | Passed | Hidden verifier | Phase-2 successful source reads | Verified read-free follow-ups | Avg model calls | Tokens in/out | Avg duration |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in contract.MEMORY_VARIANTS:
        metrics = memory["variants"].get(variant)
        if not metrics:
            continue
        lines.append(
            f"| `{variant}` | {metrics['passed']}/{metrics['attempt_count']} "
            f"| {metrics['hidden_verifier_passed']}/{metrics['attempt_count']} "
            f"| {metrics['phase_two_source_read_successful']} "
            f"| {metrics['verified_read_free_followups']} "
            f"| {metrics['avg_model_calls']:.2f} "
            f"| {metrics['total_input_tokens']}/{metrics['total_output_tokens']} "
            f"| {metrics['avg_duration_ms'] / 1000:.2f}s |"
        )
    lines.extend(
        [
            "",
            "| Paired comparison | Value |",
            "|---|---:|",
            f"| Complete pairs | {pairs['complete_pair_count']} |",
            f"| Both-passed pairs | {pairs['both_passed_pair_count']} |",
            (
                "| Working-memory phase-2 successful source reads | "
                f"{pairs['working_memory_phase_two_successful_source_reads']} |"
            ),
            (
                "| Memory-disabled phase-2 successful source reads | "
                f"{pairs['memory_disabled_phase_two_successful_source_reads']} |"
            ),
            (
                "| Pairs with fewer working-memory reads | "
                f"{pairs['pairs_with_fewer_candidate_reads']} |"
            ),
            (
                "| Working-memory − memory-disabled reads | "
                f"{pairs['successful_source_read_delta']} |"
            ),
            "",
            "## Checkpoint resume scenarios",
            "",
            "| Scenario | Expected status | Passed | Status matches | Hidden verifier | Phase-2 successful source reads |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    resume_rows_by_task = {}
    for row in artifact["rows"]:
        if row["episode_type"] == "checkpoint_resume":
            resume_rows_by_task.setdefault(row["task_id"], row)
    for task_id, metrics in resume["tasks"].items():
        sample = resume_rows_by_task[task_id]
        lines.append(
            f"| `{task_id}` | `{sample['expected_resume_status']}` "
            f"| {metrics['passed']}/{metrics['attempt_count']} "
            f"| {metrics['status_matches']}/{metrics['attempt_count']} "
            f"| {metrics['hidden_verifier_passed']}/{metrics['attempt_count']} "
            f"| {metrics['phase_two_source_read_successful']} |"
        )
    lines.extend(
        [
            "",
            "## Protocol",
            "",
            (
                "- Every episode starts from a fresh fixture. Phase two uses a fresh "
                "model client and a fresh `Pico.from_session(...)` instance; the "
                "provider tool conversation is not reused."
            ),
            (
                "- Memory variants differ only in `feature_flags.memory`; both keep "
                "the same read-only dedup guard, tool surface, context settings, "
                "model, and prompts. Phase one must read the source and return only "
                "`ACK`, so checkpoint prose cannot leak the source value to the control."
            ),
            (
                "- Resume phase one is intentionally interrupted only after a completed "
                "qualifying `read_file` result, at which point Pico has recorded a "
                "checkpoint. The expected interruption is separated from unexpected "
                "provider/model failures."
            ),
            (
                "- The hidden verifier is copied into the fixture only after phase two; "
                "workspace isolation and trace parsing must pass before it runs."
            ),
            "",
            "## Provenance",
            "",
            f"- Captured at: `{artifact['captured_at']}`",
            f"- Provider/model: `{artifact['provider']}` / `{artifact['model']}`",
            f"- Commit: `{artifact['runtime']['commit_sha']}`",
            f"- Branch: `{artifact['runtime']['branch']}`",
            f"- Working tree dirty before result write: `{artifact['runtime']['working_tree_dirty']}`",
            f"- Repetitions: `{artifact['repetitions']}`",
            f"- Fixture snapshot: `{artifact['benchmark']['fixture_snapshot_id']}`",
            f"- Evaluation snapshot: `{artifact['benchmark']['evaluation_snapshot_id']}`",
            "",
            "## Scope",
            "",
            (
                "This is a frozen engineering benchmark over small controlled fixtures, "
                "not a general coding-capability score. It reports observed phase-two "
                "file access and recovery behavior for this exact runtime/model snapshot. "
                "A successful source read count of zero is an observation, not a runtime "
                "guarantee of cross-turn read deduplication."
            ),
            "",
        ]
    )
    return "\n".join(lines)
