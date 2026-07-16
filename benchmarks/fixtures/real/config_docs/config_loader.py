def load_config(defaults, file_values, environ):
    """Merge configuration sources, with later sources taking precedence."""
    result = dict(defaults)
    result.update(file_values)
    for key in set(defaults) | set(file_values):
        environment_key = f"APP_{key.upper()}"
        if environment_key in environ:
            result[key] = environ[environment_key]
    return result
