"""Pure value helpers used by :mod:`pico.tool_runtime`."""

def intersect_write_scopes(contract_paths, policy_paths):
    if contract_paths is None:
        return policy_paths
    if policy_paths is None:
        return contract_paths
    policy = set(policy_paths)
    return tuple(path for path in contract_paths if path in policy)


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
