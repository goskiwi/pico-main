from ops_center.rollouts.service import evaluate


def evaluate_flag(
    flags,
    assignments,
    tenant_id,
    user_id,
    flag_name,
):
    """Return and persist a stable rollout decision."""
    return evaluate(
        flags,
        assignments,
        tenant_id,
        user_id,
        flag_name,
    )
