import pytest

from more_itertools import tail


@pytest.mark.parametrize("iterable", ["ABCDEFG", iter("ABCDEFG")])
def test_negative_tail_size_is_rejected_for_sized_and_iterator_inputs(iterable):
    with pytest.raises(ValueError):
        list(tail(-1, iterable))
