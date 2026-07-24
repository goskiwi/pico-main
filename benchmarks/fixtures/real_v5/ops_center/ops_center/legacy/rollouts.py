def evaluate_flag(flags, assignments, tenant_id, user_id, flag_name):
    """Deprecated process-randomized rollout."""
    return hash(user_id) % 100 < flags[flag_name]["percentage"]
