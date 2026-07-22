"""Real-model coding benchmark with hidden, Docker-isolated verification."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from .common import git_value as _git_value
from .common import safe_mean as _safe_mean
from .common import safe_ratio as _safe_ratio
from .common import utc_timestamp as _utc_timestamp
from pico.cli import DEFAULT_OPENAI_MODEL, _load_workspace_env
from pico.models import OpenAICompatibleModelClient
from pico.run_store import RunStore
from pico.runtime import Pico
from pico.sandbox import DockerSandbox, DockerSandboxConfig, SandboxResult
from pico.session_store import SessionStore
from pico.workspace import WorkspaceContext


REAL_BENCHMARK_SCHEMA_VERSION = 1
REAL_BENCHMARK_ARTIFACT_SCHEMA_VERSION = 3
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


def load_real_benchmark(path=DEFAULT_REAL_BENCHMARK_PATH, repo_root=None):
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


def _trace_events(trace_path):
    if not Path(trace_path).is_file():
        return []
    try:
        trace_text = Path(trace_path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [
            {
                "event": "trace_parse_error",
                "line_number": 0,
                "error": f"trace is not valid UTF-8: {exc}",
            }
        ]
    events = []
    for line_number, line in enumerate(
        trace_text.splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            events.append(
                {
                    "event": "trace_parse_error",
                    "line_number": line_number,
                    "error": str(exc),
                }
            )
            continue
        if not isinstance(event, dict):
            events.append(
                {
                    "event": "trace_parse_error",
                    "line_number": line_number,
                    "error": "trace record must be a JSON object",
                }
            )
            continue
        events.append(event)
    return events


def _nonnegative_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _delegate_attempt(event):
    """Validate one structured delegate tool outcome, never its text preview."""
    name = str(event.get("name", "")).strip()
    tool_status = str(event.get("tool_status", "")).strip()
    raw_outcome = event.get("delegate_outcome")
    issues = []
    if tool_status != "ok":
        issues.append(f"tool_status:{tool_status or 'missing'}")
    if not isinstance(raw_outcome, dict):
        return {
            "name": name,
            "tool_status": tool_status,
            "requested_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "items": [],
            "issues": [*issues, "missing_delegate_outcome"],
            "successful": False,
        }

    counts = {
        key: _nonnegative_int(raw_outcome.get(key))
        for key in ("requested_count", "completed_count", "failed_count")
    }
    for key, value in counts.items():
        if value is None:
            issues.append(f"invalid_{key}")
            counts[key] = 0
    raw_items = raw_outcome.get("items")
    if not isinstance(raw_items, list):
        issues.append("invalid_items")
        raw_items = []
    items = []
    for position, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            issues.append(f"invalid_item:{position}")
            continue
        item = {
            "index": raw_item.get("index"),
            "role": str(raw_item.get("role", "")).strip(),
            "status": str(raw_item.get("status", "")).strip(),
            "agent_id": str(raw_item.get("agent_id", "")).strip(),
            "child_status": str(raw_item.get("child_status", "")).strip(),
            "stop_reason": str(raw_item.get("stop_reason", "")).strip(),
        }
        items.append(item)
        if item["status"] != "ok":
            issues.append(f"child_not_completed:{position}")
        elif item["child_status"] != "completed":
            issues.append(f"child_status_not_completed:{position}")
        if item["status"] == "ok" and not item["agent_id"]:
            issues.append(f"missing_child_agent_id:{position}")

    requested_count = counts["requested_count"]
    completed_count = counts["completed_count"]
    failed_count = counts["failed_count"]
    item_completed_count = sum(item["status"] == "ok" for item in items)
    if requested_count < 1:
        issues.append("no_children_requested")
    if len(items) != requested_count:
        issues.append("requested_item_count_mismatch")
    if completed_count != item_completed_count:
        issues.append("completed_item_count_mismatch")
    if completed_count + failed_count != requested_count:
        issues.append("terminal_count_mismatch")
    if completed_count != requested_count or failed_count:
        issues.append("not_all_children_completed")
    return {
        "name": name,
        "tool_status": tool_status,
        "requested_count": requested_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "items": items,
        "issues": sorted(set(issues)),
        "successful": not issues,
    }


def _trace_metrics(trace_path):
    events = _trace_events(trace_path)
    trace_parse_errors = [
        {
            "line_number": int(event.get("line_number") or 0),
            "error": str(event.get("error", "")),
        }
        for event in events
        if event.get("event") == "trace_parse_error"
    ]
    requested_events = [
        event for event in events if event.get("event") == "model_requested"
    ]
    model_events = [event for event in events if event.get("event") == "model_parsed"]
    failed_events = [event for event in events if event.get("event") == "model_failed"]
    rejected_events = [
        event for event in events if event.get("event") == "model_action_rejected"
    ]
    executed_tools = [
        str(event.get("name", "")).strip()
        for event in events
        if event.get("event") == "tool_executed" and str(event.get("name", "")).strip()
    ]
    delegate_attempts = [
        _delegate_attempt(event)
        for event in events
        if event.get("event") == "tool_executed"
        and str(event.get("name", "")).strip() in {"delegate", "delegate_many"}
    ]
    failed_delegate_outcomes = [
        attempt["name"] for attempt in delegate_attempts if not attempt["successful"]
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
    model_duration_ms = sum(
        int(event.get("duration_ms") or 0) for event in (*model_events, *failed_events)
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
        "model_duration_ms": model_duration_ms,
        "model_failures": len(failed_events),
        "model_action_rejections": len(rejected_events),
        "action_protocols": action_protocols,
        "executed_tools": executed_tools,
        "delegate_attempts": delegate_attempts,
        "failed_delegate_outcomes": failed_delegate_outcomes,
        "trace_parse_errors": trace_parse_errors,
    }


_TRACE_AGGREGATE_FIELDS = (
    "model_calls",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "model_duration_ms",
    "model_failures",
    "model_action_rejections",
)


def _attempt_trace_metrics(parent_run_dir, run_dirs, workspace_root):
    """Aggregate one new parent run and only its new, related delegate runs.

    ``run_dirs`` is the directory snapshot delta captured around one benchmark
    attempt. Delegate candidates must also be immediate children of the same
    RunStore root, use the same workspace root, and form an agent-parent chain
    rooted at the explicit parent run. This keeps historical and unrelated
    concurrent runs out of the attempt's cost totals.
    """
    parent_run_dir = Path(parent_run_dir)
    runs_root = parent_run_dir.parent.resolve()
    expected_workspace_root = Path(workspace_root).resolve()
    scoped_run_dirs = []
    for candidate in run_dirs:
        candidate = Path(candidate)
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        resolved = candidate.resolve()
        if resolved.parent != runs_root or not _path_is_within(resolved, runs_root):
            continue
        scoped_run_dirs.append(resolved)

    parent_resolved = parent_run_dir.resolve()
    identities = {}
    for run_dir in scoped_run_dirs:
        started = next(
            (
                event
                for event in _trace_events(run_dir / "trace.jsonl")
                if event.get("event") == "run_started"
            ),
            None,
        )
        if started is None:
            continue
        actual_workspace_root = str(started.get("workspace_root", "")).strip()
        if not actual_workspace_root:
            continue
        if Path(actual_workspace_root).resolve() != expected_workspace_root:
            continue
        agent_id = str(started.get("agent_id", "")).strip()
        if not agent_id:
            continue
        try:
            depth = int(started.get("depth", 0))
        except (TypeError, ValueError):
            continue
        identities[run_dir] = {
            "agent_id": agent_id,
            "parent_agent_id": str(started.get("parent_agent_id", "")).strip(),
            "depth": depth,
        }

    parent_identity = identities.get(parent_resolved)
    delegate_run_dirs = []
    if parent_identity is not None:
        related_agents = {
            parent_identity["agent_id"]: parent_identity["depth"],
        }
        pending = {
            run_dir: identity
            for run_dir, identity in identities.items()
            if run_dir != parent_resolved
        }
        while pending:
            admitted = []
            for run_dir, identity in pending.items():
                parent_depth = related_agents.get(identity["parent_agent_id"])
                if parent_depth is None or identity["depth"] != parent_depth + 1:
                    continue
                admitted.append((run_dir, identity))
            if not admitted:
                break
            for run_dir, identity in admitted:
                delegate_run_dirs.append(run_dir)
                related_agents[identity["agent_id"]] = identity["depth"]
                pending.pop(run_dir)

    parent_metrics = _trace_metrics(parent_resolved / "trace.jsonl")
    delegate_metrics = {field: 0 for field in _TRACE_AGGREGATE_FIELDS}
    delegate_action_protocols = set()
    delegate_trace_parse_errors = []
    for run_dir in delegate_run_dirs:
        child_metrics = _trace_metrics(run_dir / "trace.jsonl")
        for field in _TRACE_AGGREGATE_FIELDS:
            delegate_metrics[field] += int(child_metrics[field])
        delegate_action_protocols.update(child_metrics["action_protocols"])
        delegate_trace_parse_errors.extend(
            {**error, "run_id": run_dir.name}
            for error in child_metrics["trace_parse_errors"]
        )
    delegate_metrics["action_protocols"] = sorted(delegate_action_protocols)
    delegate_metrics["trace_parse_errors"] = delegate_trace_parse_errors

    total_metrics = {
        field: int(parent_metrics[field]) + delegate_metrics[field]
        for field in _TRACE_AGGREGATE_FIELDS
    }
    total_metrics["action_protocols"] = sorted(
        set(parent_metrics["action_protocols"]) | delegate_action_protocols
    )
    parent_trace_parse_errors = [
        {**error, "run_id": parent_resolved.name}
        for error in parent_metrics["trace_parse_errors"]
    ]
    parent_metrics["trace_parse_errors"] = parent_trace_parse_errors
    total_metrics["trace_parse_errors"] = [
        *parent_trace_parse_errors,
        *delegate_trace_parse_errors,
    ]
    return {
        "parent": parent_metrics,
        "delegate": delegate_metrics,
        "total": total_metrics,
        "delegate_run_count": len(delegate_run_dirs),
        "delegate_run_ids": sorted(path.name for path in delegate_run_dirs),
        "delegate_agent_ids": sorted(
            identities[path]["agent_id"] for path in delegate_run_dirs
        ),
    }


def _evaluate_delegate_evidence(
    trace_metrics,
    *,
    delegate_run_count,
    delegate_agent_ids,
    required,
    expected_delegate_runs=None,
    expected_delegate_attempts=None,
):
    """Cross-check parent tool metadata against related child run identities."""
    if (
        not required
        and expected_delegate_runs is None
        and expected_delegate_attempts is None
    ):
        return {
            "ok": True,
            "required": False,
            "expected_delegate_runs": None,
            "expected_delegate_attempts": None,
            "attempt_count": 0,
            "requested_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "successful_attempt_count": 0,
            "reported_agent_ids": [],
            "related_agent_ids": sorted(delegate_agent_ids),
            "issues": [],
        }

    attempts = list(trace_metrics.get("delegate_attempts") or [])
    issues = [
        f"{attempt.get('name') or 'delegate'}:{issue}"
        for attempt in attempts
        for issue in attempt.get("issues", [])
    ]
    requested_count = sum(int(item.get("requested_count") or 0) for item in attempts)
    completed_count = sum(int(item.get("completed_count") or 0) for item in attempts)
    failed_count = sum(int(item.get("failed_count") or 0) for item in attempts)
    successful_attempt_count = sum(bool(item.get("successful")) for item in attempts)
    reported_agent_ids = sorted(
        str(item.get("agent_id", "")).strip()
        for attempt in attempts
        for item in attempt.get("items", [])
        if str(item.get("agent_id", "")).strip()
    )
    related_agent_ids = sorted(str(agent_id) for agent_id in delegate_agent_ids)

    if successful_attempt_count < 1:
        issues.append("no_successful_delegate")
    if completed_count != delegate_run_count:
        issues.append("completed_run_count_mismatch")
    if reported_agent_ids != related_agent_ids:
        issues.append("delegate_agent_identity_mismatch")
    if expected_delegate_runs is not None:
        expected = int(expected_delegate_runs)
        if requested_count != expected:
            issues.append("expected_requested_count_mismatch")
        if completed_count != expected:
            issues.append("expected_completed_count_mismatch")
        if delegate_run_count != expected:
            issues.append("expected_delegate_run_count_mismatch")
    if expected_delegate_attempts is not None and len(attempts) != int(
        expected_delegate_attempts
    ):
        issues.append("expected_delegate_attempt_count_mismatch")

    issues = sorted(set(issues))
    return {
        "ok": not issues,
        "required": bool(required),
        "expected_delegate_runs": expected_delegate_runs,
        "expected_delegate_attempts": expected_delegate_attempts,
        "attempt_count": len(attempts),
        "requested_count": requested_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "successful_attempt_count": successful_attempt_count,
        "reported_agent_ids": reported_agent_ids,
        "related_agent_ids": related_agent_ids,
        "issues": issues,
    }


def _path_is_within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _workspace_isolation_audit(workspace_root, run_dirs, task):
    """Audit every parent/child run before hidden verifier files are installed."""
    expected_root = Path(workspace_root).resolve()
    run_dirs = sorted((Path(path) for path in run_dirs), key=lambda path: path.name)
    violations = []
    seen_violations = set()
    verifier_markers = sorted(
        {
            str(item.get("source", "")).strip().replace("\\", "/")
            for item in task.get("verifier_files", [])
            if str(item.get("source", "")).strip()
        }
    )

    def add_violation(kind, run_id, **details):
        violation = {"type": kind, "run_id": str(run_id), **details}
        key = json.dumps(violation, sort_keys=True)
        if key not in seen_violations:
            seen_violations.add(key)
            violations.append(violation)

    if not run_dirs:
        add_violation("missing_runs", "")

    for run_dir in run_dirs:
        run_id = run_dir.name
        trace_path = run_dir / "trace.jsonl"
        events = []
        if trace_path.is_file():
            for event in _trace_events(trace_path):
                if event.get("event") == "trace_parse_error":
                    add_violation(
                        "invalid_trace",
                        run_id,
                        line_number=int(event.get("line_number") or 0),
                        error=str(event.get("error", "")),
                    )
                    continue
                events.append(event)
        started = next(
            (event for event in events if event.get("event") == "run_started"),
            None,
        )
        if started is None:
            add_violation("missing_run_started", run_id)
        else:
            actual_root = str(started.get("workspace_root", "")).strip()
            if not actual_root:
                add_violation("missing_workspace_root", run_id)
            elif Path(actual_root).resolve() != expected_root:
                add_violation(
                    "workspace_root_mismatch",
                    run_id,
                    expected=str(expected_root),
                    actual=str(Path(actual_root).resolve()),
                )
        if not any(event.get("event") == "run_finished" for event in events):
            add_violation("unfinished_run", run_id)

        for event in events:
            if event.get("event") != "tool_executed":
                continue
            name = str(event.get("name", "")).strip()
            if name not in {
                "list_files",
                "read_file",
                "search",
                "write_file",
                "patch_file",
            }:
                continue
            raw_path = str((event.get("args") or {}).get("path", ".")).strip() or "."
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = expected_root / candidate
            if not _path_is_within(candidate, expected_root):
                add_violation(
                    "tool_path_outside_workspace",
                    run_id,
                    tool=name,
                    path=raw_path,
                )

        artifact_paths = [trace_path, run_dir / "report.json"]
        artifact_paths.extend(sorted((run_dir / "tool_outputs").glob("*.txt")))
        for artifact_path in artifact_paths:
            if not artifact_path.is_file():
                continue
            text = artifact_path.read_text(encoding="utf-8", errors="replace").replace(
                "\\", "/"
            )
            for marker in verifier_markers:
                if marker in text:
                    add_violation(
                        "verifier_source_exposed",
                        run_id,
                        artifact=str(artifact_path.relative_to(run_dir)),
                        marker=marker,
                    )
            if "_delegate" in artifact_path.name and "status=timeout" in text:
                add_violation(
                    "delegate_not_quiescent",
                    run_id,
                    artifact=str(artifact_path.relative_to(run_dir)),
                )

        for output_path in sorted((run_dir / "tool_outputs").glob("*_search.txt")):
            for line in output_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                match = re.match(r"^(/.+?):\d+:", line)
                if not match:
                    continue
                result_path = Path(match.group(1))
                if not _path_is_within(result_path, expected_root):
                    add_violation(
                        "search_result_outside_workspace",
                        run_id,
                        path=str(result_path),
                    )

    return {
        "ok": not violations,
        "expected_workspace_root": str(expected_root),
        "run_count": len(run_dirs),
        "run_ids": [path.name for path in run_dirs],
        "violations": violations,
    }


def _failure_category(
    task_state,
    verifier_result,
    report,
    workspace_isolation_violations=(),
    missing_required_tools=(),
    failed_delegate_outcomes=(),
    trace_parse_errors=(),
):
    if workspace_isolation_violations:
        return "workspace_isolation_failed"
    if missing_required_tools:
        return "required_tool_missing"
    if failed_delegate_outcomes:
        return "delegate_outcome_failed"
    if trace_parse_errors:
        return "trace_parse_error"
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


def _scoped_row_metric(row, scope, metric):
    """Read schema-v3 aggregate fields with a parent-only fallback for old rows."""
    scoped_key = f"{scope}_{metric}"
    if scoped_key in row:
        return int(row[scoped_key])
    if scope == "delegate":
        return 0
    return int(row.get(metric, 0))


def _scoped_row_protocols(row, scope):
    scoped_key = f"{scope}_action_protocols"
    if scoped_key in row:
        return list(row[scoped_key])
    if scope == "delegate":
        return []
    return list(row.get("action_protocols", []))


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
                        _scoped_row_metric(row, "total", "model_calls")
                        for row in repetition_rows
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
            "avg_parent_model_calls": _safe_mean(
                _scoped_row_metric(row, "parent", "model_calls") for row in variant_rows
            ),
            "avg_delegate_model_calls": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_calls")
                for row in variant_rows
            ),
            "avg_total_model_calls": _safe_mean(
                _scoped_row_metric(row, "total", "model_calls") for row in variant_rows
            ),
            "avg_model_calls": _safe_mean(
                _scoped_row_metric(row, "total", "model_calls") for row in variant_rows
            ),
            "avg_parent_model_failures": _safe_mean(
                _scoped_row_metric(row, "parent", "model_failures")
                for row in variant_rows
            ),
            "avg_delegate_model_failures": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_failures")
                for row in variant_rows
            ),
            "avg_total_model_failures": _safe_mean(
                _scoped_row_metric(row, "total", "model_failures")
                for row in variant_rows
            ),
            "avg_model_failures": _safe_mean(
                _scoped_row_metric(row, "total", "model_failures")
                for row in variant_rows
            ),
            "avg_parent_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "parent", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_delegate_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_total_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "total", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_model_action_rejections": _safe_mean(
                _scoped_row_metric(row, "total", "model_action_rejections")
                for row in variant_rows
            ),
            "avg_agent_duration_ms": _safe_mean(
                row["agent_duration_ms"] for row in variant_rows
            ),
            "avg_total_duration_ms": _safe_mean(
                row["total_duration_ms"] for row in variant_rows
            ),
            "avg_delegate_run_count": _safe_mean(
                int(row.get("delegate_run_count", 0)) for row in variant_rows
            ),
            "total_delegate_run_count": sum(
                int(row.get("delegate_run_count", 0)) for row in variant_rows
            ),
            "delegate_run_count": sum(
                int(row.get("delegate_run_count", 0)) for row in variant_rows
            ),
            "total_parent_input_tokens": sum(
                _scoped_row_metric(row, "parent", "input_tokens")
                for row in variant_rows
            ),
            "total_delegate_input_tokens": sum(
                _scoped_row_metric(row, "delegate", "input_tokens")
                for row in variant_rows
            ),
            "total_input_tokens": sum(
                _scoped_row_metric(row, "total", "input_tokens") for row in variant_rows
            ),
            "total_parent_output_tokens": sum(
                _scoped_row_metric(row, "parent", "output_tokens")
                for row in variant_rows
            ),
            "total_delegate_output_tokens": sum(
                _scoped_row_metric(row, "delegate", "output_tokens")
                for row in variant_rows
            ),
            "total_output_tokens": sum(
                _scoped_row_metric(row, "total", "output_tokens")
                for row in variant_rows
            ),
            "total_parent_cached_tokens": sum(
                _scoped_row_metric(row, "parent", "cached_tokens")
                for row in variant_rows
            ),
            "total_delegate_cached_tokens": sum(
                _scoped_row_metric(row, "delegate", "cached_tokens")
                for row in variant_rows
            ),
            "total_cached_tokens": sum(
                _scoped_row_metric(row, "total", "cached_tokens")
                for row in variant_rows
            ),
            "avg_parent_model_duration_ms": _safe_mean(
                _scoped_row_metric(row, "parent", "model_duration_ms")
                for row in variant_rows
            ),
            "avg_delegate_model_duration_ms": _safe_mean(
                _scoped_row_metric(row, "delegate", "model_duration_ms")
                for row in variant_rows
            ),
            "avg_total_model_duration_ms": _safe_mean(
                _scoped_row_metric(row, "total", "model_duration_ms")
                for row in variant_rows
            ),
            "parent_action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "parent")
                }
            ),
            "delegate_action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "delegate")
                }
            ),
            "total_action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "total")
                }
            ),
            "action_protocols": sorted(
                {
                    protocol
                    for row in variant_rows
                    for protocol in _scoped_row_protocols(row, "total")
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
            value not in SUPPORTED_VARIANTS for value in self.variants
        ):
            raise ValueError(
                f"variants must be drawn from: {', '.join(SUPPORTED_VARIANTS)}"
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
        summary = summarize_real_rows(rows)
        git_status = _git_value(
            ["status", "--porcelain", "--untracked-files=all"],
            cwd=self.repo_root,
            fallback=None,
            preserve_empty=True,
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
        attempt_trace = _attempt_trace_metrics(
            run_store.run_dir(task_state),
            run_dirs,
            workspace_root,
        )
        trace = attempt_trace["parent"]
        workspace_isolation = _workspace_isolation_audit(
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
        delegate_evidence = _evaluate_delegate_evidence(
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
        failure_category = _failure_category(
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


def _artifact_model_cost_scope(artifact):
    configured = str(
        (artifact.get("run_config") or {}).get("model_cost_scope", "")
    ).strip()
    if configured:
        return configured
    if int(artifact.get("schema_version", 0) or 0) >= 3:
        return "attempt_parent_and_related_delegates"
    return "parent_run_only"


def render_real_benchmark_markdown(artifact):
    summary = artifact["summary"]
    benchmark_name = artifact["benchmark"].get("name") or "Pico Real-world Benchmark"
    model_cost_scope = _artifact_model_cost_scope(artifact)
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
        f"- Model cost scope: `{model_cost_scope}`",
        (
            "- Duration semantics: model time is cumulative across model calls; "
            "agent duration is parent-attempt wall time and already includes delegate wait"
        ),
        (
            f"- Sandbox: `{artifact['sandbox']['image']}`, {artifact['sandbox']['cpus']} CPU, "
            f"{artifact['sandbox']['memory']} memory, {artifact['sandbox']['pids_limit']} PIDs"
        ),
        "",
        "## Results",
        "",
        "| Variant | Protocols (all) | Pass rate | Passed | Avg tools | Avg calls P/D/T | Avg delegates | Avg failures P/D/T | Avg rejects P/D/T | Input P/D/T | Cached P/D/T | Output P/D/T | Model time P/D/T | Avg duration |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, metrics in summary["variants"].items():
        avg_model_calls = metrics["avg_model_calls"]
        parent_calls = metrics.get("avg_parent_model_calls", avg_model_calls)
        delegate_calls = metrics.get("avg_delegate_model_calls", 0.0)
        total_calls = metrics.get("avg_total_model_calls", avg_model_calls)
        parent_failures = metrics.get("avg_parent_model_failures", 0.0)
        delegate_failures = metrics.get("avg_delegate_model_failures", 0.0)
        total_failures = metrics.get("avg_total_model_failures", parent_failures)
        avg_rejections = metrics.get("avg_model_action_rejections", 0.0)
        parent_rejections = metrics.get(
            "avg_parent_model_action_rejections", avg_rejections
        )
        delegate_rejections = metrics.get("avg_delegate_model_action_rejections", 0.0)
        total_rejections = metrics.get(
            "avg_total_model_action_rejections", avg_rejections
        )
        attempt_count = max(1, int(metrics.get("attempt_count", 1)))
        avg_delegate_runs = metrics.get(
            "avg_delegate_run_count",
            metrics.get("delegate_run_count", 0) / attempt_count,
        )
        parent_input = metrics.get(
            "total_parent_input_tokens", metrics["total_input_tokens"]
        )
        delegate_input = metrics.get("total_delegate_input_tokens", 0)
        parent_cached = metrics.get(
            "total_parent_cached_tokens", metrics["total_cached_tokens"]
        )
        delegate_cached = metrics.get("total_delegate_cached_tokens", 0)
        parent_output = metrics.get(
            "total_parent_output_tokens", metrics["total_output_tokens"]
        )
        delegate_output = metrics.get("total_delegate_output_tokens", 0)
        parent_model_duration_ms = metrics.get("avg_parent_model_duration_ms", 0.0)
        delegate_model_duration_ms = metrics.get("avg_delegate_model_duration_ms", 0.0)
        total_model_duration_ms = metrics.get(
            "avg_total_model_duration_ms", parent_model_duration_ms
        )
        lines.append(
            f"| {variant} | {', '.join(metrics.get('action_protocols', [])) or '-'} "
            f"| {metrics['pass_rate']:.1%} | {metrics['passed']}/{metrics.get('attempt_count', metrics['task_count'])} "
            f"| {metrics['avg_tool_steps']:.2f} "
            f"| {parent_calls:.2f}/{delegate_calls:.2f}/{total_calls:.2f} "
            f"| {avg_delegate_runs:.2f} "
            f"| {parent_failures:.2f}/{delegate_failures:.2f}/{total_failures:.2f} "
            f"| {parent_rejections:.2f}/{delegate_rejections:.2f}/{total_rejections:.2f} "
            f"| {parent_input}/{delegate_input}/{metrics['total_input_tokens']} "
            f"| {parent_cached}/{delegate_cached}/{metrics['total_cached_tokens']} "
            f"| {parent_output}/{delegate_output}/{metrics['total_output_tokens']} "
            f"| {parent_model_duration_ms / 1000:.2f}s/"
            f"{delegate_model_duration_ms / 1000:.2f}s/"
            f"{total_model_duration_ms / 1000:.2f}s "
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
            "| Task | Rep | Category | Variant | Result | Isolation | Tools | Delegates | Calls P/D/T | Failures P/D/T | Rejects P/D/T | Model time P/D/T | Duration | Failure |",
            "|---|---:|---|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in artifact["rows"]:
        result = "PASS" if row["passed"] else "FAIL"
        isolation = (
            "PASS" if row.get("workspace_isolation", {}).get("ok", True) else "FAIL"
        )
        parent_calls = _scoped_row_metric(row, "parent", "model_calls")
        delegate_calls = _scoped_row_metric(row, "delegate", "model_calls")
        total_calls = _scoped_row_metric(row, "total", "model_calls")
        parent_failures = _scoped_row_metric(row, "parent", "model_failures")
        delegate_failures = _scoped_row_metric(row, "delegate", "model_failures")
        total_failures = _scoped_row_metric(row, "total", "model_failures")
        parent_rejections = _scoped_row_metric(row, "parent", "model_action_rejections")
        delegate_rejections = _scoped_row_metric(
            row, "delegate", "model_action_rejections"
        )
        total_rejections = _scoped_row_metric(row, "total", "model_action_rejections")
        parent_model_duration_ms = _scoped_row_metric(
            row, "parent", "model_duration_ms"
        )
        delegate_model_duration_ms = _scoped_row_metric(
            row, "delegate", "model_duration_ms"
        )
        total_model_duration_ms = _scoped_row_metric(row, "total", "model_duration_ms")
        lines.append(
            f"| {row['task_id']} | {row.get('repetition', 1)} | {row['category']} | {row['variant']} | {result} "
            f"| {isolation} | {row['tool_steps']} "
            f"| {row.get('delegate_run_count', 0)} "
            f"| {parent_calls}/{delegate_calls}/{total_calls} "
            f"| {parent_failures}/{delegate_failures}/{total_failures} "
            f"| {parent_rejections}/{delegate_rejections}/{total_rejections} "
            f"| {parent_model_duration_ms / 1000:.2f}s/"
            f"{delegate_model_duration_ms / 1000:.2f}s/"
            f"{total_model_duration_ms / 1000:.2f}s | "
            f"{row['total_duration_ms'] / 1000:.2f}s "
            f"| {row['failure_category'] or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Scope boundary",
            "",
            "- These are real model runs over fresh repository copies; hidden verifier tests are injected only after the agent stops.",
            "- Parent and child run roots, file-tool paths, search results, and verifier-source exposure are audited before hidden verifier injection; failures skip verification.",
            "- In schema v3, compatibility fields for model calls, tokens, failures, rejections, and protocols cover the parent plus related delegates; explicit P/D/T fields retain the breakdown.",
            "- Required/executed tools and structured delegate outcomes remain parent-trace checks; related child identities and completion are cross-checked from child traces, whose model events also contribute to aggregate behavior and cost metrics.",
            "- Cumulative model-call duration is a workload indicator, not wall latency; concurrent child durations can overlap. Agent duration is the parent attempt's end-to-end wall time.",
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
    baseline_cost_scope = _artifact_model_cost_scope(baseline)
    candidate_cost_scope = _artifact_model_cost_scope(candidate)
    if baseline_cost_scope != candidate_cost_scope:
        raise ValueError("benchmark model-cost scopes do not match")
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
        "model_cost_scope": baseline_cost_scope,
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
        f"- Model cost scope: `{comparison.get('model_cost_scope', 'parent_run_only')}`",
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
