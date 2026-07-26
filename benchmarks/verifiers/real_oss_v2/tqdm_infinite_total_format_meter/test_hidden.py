from tqdm.std import tqdm


def test_format_meter_treats_infinite_total_as_unknown():
    assert tqdm.format_meter(5, float("inf"), 1) == tqdm.format_meter(5, None, 1)
