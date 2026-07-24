from copy import deepcopy

import pytest

from ops_center.scheduling.api import is_maintenance_active


def test_cross_midnight_segment_belongs_to_previous_start_day():
    windows = {
        "database": {
            "days": {0},
            "start": 1380,
            "end": 60,
        }
    }

    assert is_maintenance_active(windows, "database", 0, 1400)
    assert is_maintenance_active(windows, "database", 1, 30)
    assert not is_maintenance_active(windows, "database", 1, 60)
    assert not is_maintenance_active(windows, "database", 2, 30)


def test_offset_is_applied_before_day_and_window_matching():
    windows = {
        "search": {
            "days": {0},
            "start": 30,
            "end": 90,
        }
    }

    assert is_maintenance_active(
        windows,
        "search",
        6,
        1380,
        offset_minutes=120,
    )
    assert not is_maintenance_active(
        windows,
        "search",
        6,
        1380,
        offset_minutes=30,
    )


def test_normal_window_keeps_inclusive_start_and_exclusive_end():
    windows = {
        "api": {
            "days": {4},
            "start": 300,
            "end": 360,
        }
    }

    assert is_maintenance_active(windows, "api", 4, 300)
    assert not is_maintenance_active(windows, "api", 4, 360)


@pytest.mark.parametrize(
    ("weekday", "minute"),
    [(-1, 10), (7, 10), (0, -1), (0, 1440)],
)
def test_invalid_clock_input_raises_without_mutation(weekday, minute):
    windows = {
        "api": {
            "days": {0},
            "start": 10,
            "end": 20,
        }
    }
    before = deepcopy(windows)

    with pytest.raises(ValueError):
        is_maintenance_active(windows, "api", weekday, minute)

    assert windows == before


def test_unknown_service_raises_key_error():
    with pytest.raises(KeyError):
        is_maintenance_active({}, "missing", 0, 0)
