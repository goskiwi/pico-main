from priority_queue import StablePriorityQueue


def test_equal_priority_items_are_fifo():
    queue = StablePriorityQueue()
    queue.push("z-first", priority=5)
    queue.push("a-second", priority=5)
    queue.push("m-third", priority=5)
    assert [queue.pop(), queue.pop(), queue.pop()] == ["z-first", "a-second", "m-third"]


def test_equal_priority_items_do_not_need_to_be_comparable():
    queue = StablePriorityQueue()
    first = {"id": 1}
    second = {"id": 2}
    queue.push(first, priority=1)
    queue.push(second, priority=1)
    assert queue.pop() is first
    assert queue.pop() is second
    assert len(queue) == 0
