import pytest

from priority_queue import StablePriorityQueue


def test_lower_number_has_higher_priority():
    queue = StablePriorityQueue()
    queue.push("later", priority=10)
    queue.push("first", priority=1)
    assert queue.pop() == "first"


def test_pop_empty_queue_raises_index_error():
    with pytest.raises(IndexError):
        StablePriorityQueue().pop()
