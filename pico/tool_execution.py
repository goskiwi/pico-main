"""Pure value helpers used by :mod:`pico.tool_runtime`."""

import json

DEFAULT_TOOL_PREVIEW_BYTES = 12 * 1024


def _complete_lines_within_budget(lines, budget):
    selected = []
    used = 0
    for line in lines:
        encoded_size = len(line.encode("utf-8")) + (1 if selected else 0)
        if used + encoded_size > budget:
            break
        selected.append(line)
        used += encoded_size
    return selected


def model_tool_output(content, descriptor):
    content = str(content)
    total_bytes = len(content.encode("utf-8"))
    limit = DEFAULT_TOOL_PREVIEW_BYTES
    if total_bytes <= limit:
        return content
    if not descriptor.get("artifact_id"):
        raise RuntimeError("truncated tool output requires an artifact")
    lines = content.splitlines()
    selected = _complete_lines_within_budget(lines, limit - 512)
    start_line = 1
    end_line = len(selected)
    preview = "\n".join(selected)
    notice = (
        f"[Output truncated: showing lines {start_line}-{end_line} of {len(lines)}; "
        f"full_bytes={total_bytes}; artifact_id={descriptor['artifact_id']}. "
        "Use read_artifact with this artifact_id and offset=0 to inspect the full "
        "output in 8 KiB pages.]"
    )
    return "\n".join(part for part in (preview, notice) if part)


def intersect_write_scopes(contract_paths, policy_paths):
    if contract_paths is None:
        return policy_paths
    if policy_paths is None:
        return contract_paths
    policy = set(policy_paths)
    return tuple(path for path in contract_paths if path in policy)


def redact_structured(value, redactor):
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, dict):
        return {
            str(key): redact_structured(item, redactor)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_structured(item, redactor) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redactor(str(value))


def repeat_key(run_id, name, args):
    try:
        args_signature = json.dumps(
            dict(args),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None
    return str(run_id), str(name), args_signature


def tracked_workspace_drift(states, effect_scope, tracked_files):
    if effect_scope != "workspace":
        return ()
    drift = []
    for path, actual_state in sorted(states.items()):
        change = tracked_files.get(path)
        if change is None:
            continue
        projected_state = str(change.current_after_state)
        if projected_state != actual_state:
            drift.append(
                {
                    "path": path,
                    "projected_state": projected_state,
                    "actual_state": actual_state,
                }
            )
    return tuple(drift)


def attach_preimage_artifacts(structured, preimages):
    structured = dict(structured or {})
    transitions = []
    for item in structured.get("path_transitions", ()):
        transition = dict(item)
        path = str(transition.get("path", ""))
        transition["before_artifact_id"] = str(
            preimages[path]
            if path in preimages
            else transition.get("before_artifact_id", "")
        )
        transitions.append(transition)
    if transitions:
        structured["path_transitions"] = transitions
    return structured


def effect_diff(before, after):
    return [
        path
        for path in sorted(set(before) | set(after))
        if before.get(path, "absent") != after.get(path, "absent")
    ]


def path_transitions(before, after, preimages, paths):
    return [
        {
            "path": path,
            "before_state": before[path],
            "after_state": after[path],
            "before_artifact_id": preimages.get(path, ""),
        }
        for path in paths
    ]


def classify_runner_result(failure, affected_paths, effect_scope):
    paths = list(affected_paths)
    unknown = failure is not None and not paths and effect_scope != "none"
    status = (
        "success"
        if failure is None
        else ("partial_success" if paths or unknown else "error")
    )
    side_effect = (
        "partial"
        if failure is not None and paths
        else ("unknown" if unknown else ("changed" if paths else "none"))
    )
    return status, side_effect, paths
