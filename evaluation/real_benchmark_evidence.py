"""Trace accounting and fail-closed evidence checks for real benchmarks."""

from __future__ import annotations

import json
import re
from pathlib import Path


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
    for line_number, line in enumerate(trace_text.splitlines(), start=1):
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
