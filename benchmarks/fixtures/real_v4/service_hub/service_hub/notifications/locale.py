def template_for(templates, locale):
    """Return the exact locale template or the default."""
    if locale in templates:
        return templates[locale]
    return templates["default"]
