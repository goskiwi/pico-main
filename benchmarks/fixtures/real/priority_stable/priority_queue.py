import heapq


class StablePriorityQueue:
    def __init__(self):
        self._items = []

    def push(self, item, priority):
        heapq.heappush(self._items, (priority, item))

    def pop(self):
        _priority, item = heapq.heappop(self._items)
        return item

    def __len__(self):
        return len(self._items)
