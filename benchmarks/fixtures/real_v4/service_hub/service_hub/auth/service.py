from service_hub.auth.policy import collect_permissions


def effective_permissions(role_names, role_definitions):
    """Return all permissions granted by the supplied roles."""
    return collect_permissions(role_names, role_definitions)
