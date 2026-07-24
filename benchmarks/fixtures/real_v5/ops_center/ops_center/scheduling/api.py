from ops_center.scheduling.service import maintenance_active


def is_maintenance_active(
    windows,
    service,
    weekday,
    minute,
    offset_minutes=0,
):
    """Evaluate a service maintenance window in local time."""
    return maintenance_active(
        windows,
        service,
        weekday,
        minute,
        offset_minutes,
    )
