MINUTES_PER_DAY = 1440
DAYS_PER_WEEK = 7


def local_clock(weekday, minute, offset_minutes):
    absolute = weekday * MINUTES_PER_DAY + minute + int(offset_minutes)
    local_day = (absolute // MINUTES_PER_DAY) % DAYS_PER_WEEK
    local_minute = absolute % MINUTES_PER_DAY
    return local_day, local_minute
