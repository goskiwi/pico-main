def window_contains(window, weekday, minute):
    """Experimental inclusive-end calendar matcher."""
    return weekday in window["days"] and window["start"] <= minute <= window["end"]
