def is_maintenance_active(windows, service, weekday, minute):
    """Deprecated UTC-only maintenance check."""
    window = windows[service]
    return window["start"] <= minute <= window["end"]
