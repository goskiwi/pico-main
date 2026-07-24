from service_hub.auth.service import effective_permissions


def authorize(user, permission, role_definitions):
    """Return whether a serialized user has a permission."""
    return permission in effective_permissions(user["roles"], role_definitions)
