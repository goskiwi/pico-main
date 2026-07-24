from ops_center.scheduling.calendar import window_contains
from ops_center.scheduling.timezones import local_clock


def maintenance_active(
    windows,
    service,
    weekday,
    minute,
    offset_minutes,
):
    if weekday not in range(7):
        raise ValueError("weekday must be in 0..6")
    if minute not in range(1440):
        raise ValueError("minute must be in 0..1439")
    window = windows[service]
    local_day, local_minute = local_clock(
        weekday,
        minute,
        offset_minutes,
    )
    return window_contains(window, local_day, local_minute)
