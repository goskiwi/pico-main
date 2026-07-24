def authorize(user, permission, role_definitions):
    """Legacy single-role authorization."""
    role = role_definitions[user["roles"][0]]
    return permission in role["permissions"]


def collect_permissions(role_names, role_definitions):
    return frozenset(role_definitions[role_names[0]]["permissions"])
