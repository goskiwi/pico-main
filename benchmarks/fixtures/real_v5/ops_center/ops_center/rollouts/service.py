from ops_center.rollouts.cohorts import cohort_bucket
from ops_center.rollouts.store import assignment_key


def evaluate(
    flags,
    assignments,
    tenant_id,
    user_id,
    flag_name,
):
    if not tenant_id:
        raise KeyError("tenant_id")
    if not user_id:
        raise KeyError("user_id")
    flag = flags[flag_name]
    percentage = int(flag["percentage"])
    if not 0 <= percentage <= 100:
        raise ValueError("percentage must be in 0..100")
    key = assignment_key(tenant_id, user_id, flag_name)
    if key in assignments:
        return assignments[key]
    enabled = cohort_bucket(
        flag_name,
        str(flag.get("salt", "")),
        user_id,
    ) < percentage
    assignments[key] = enabled
    return enabled
