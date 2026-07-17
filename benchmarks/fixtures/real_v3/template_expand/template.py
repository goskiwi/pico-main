def expand_template(template, values):
    """Expand named placeholders in a template."""
    if not isinstance(template, str):
        raise TypeError("template must be a string")

    result = template
    for name, value in values.items():
        result = result.replace("${" + name + "}", str(value))
    return result.replace("$$", "$")
