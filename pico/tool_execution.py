"""Pure value helpers used by :mod:`pico.tool_runtime`."""

def intersect_write_scopes(contract_paths, policy_paths):
    if contract_paths is None:
        return policy_paths
    if policy_paths is None:
        return contract_paths
    policy = set(policy_paths)
    return tuple(path for path in contract_paths if path in policy)


def effect_diff(before, after):
    return [
        path
        for path in sorted(set(before) | set(after))
        if before.get(path, "absent") != after.get(path, "absent")
    ]


def path_transitions(before, after, paths):
    return [
        {
            "path": path,
            "before_state": before[path],
            "after_state": after[path],
        }
        for path in paths
    ]


def classify_runner_result(failure, affected_paths, effect_scope):
    paths = list(affected_paths)
    unknown = not paths and effect_scope != "none"
    status = (
        "success"
        if failure is None
        else ("partial_success" if paths else "error")
    )
    side_effect = (
        "partial"
        if failure is not None and paths
        else ("unknown" if unknown else ("changed" if paths else "none"))
    )
    return status, side_effect, paths
