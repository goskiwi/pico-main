from ops_center.scheduling.api import is_maintenance_active


def test_daytime_window_matches():
    windows = {
        "search": {
            "days": {2},
            "start": 600,
            "end": 660,
        }
    }

    assert is_maintenance_active(windows, "search", 2, 630)
    assert not is_maintenance_active(windows, "search", 2, 660)
