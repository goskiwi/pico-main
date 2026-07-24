def window_contains(window, weekday, minute):
    start = int(window["start"])
    end = int(window["end"])
    return weekday in window["days"] and start <= minute < end
