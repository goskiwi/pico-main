class NameIndex:
    def __init__(self):
        self._by_id = {}
        self._by_name = {}

    def add(self, item_id, name):
        if item_id in self._by_id:
            raise ValueError("duplicate item id")
        if name in self._by_name:
            raise ValueError("duplicate name")
        self._by_id[item_id] = name
        self._by_name[name] = item_id

    def rename(self, item_id, new_name):
        old_name = self._by_id[item_id]
        self._by_id[item_id] = new_name
        if new_name in self._by_name:
            raise ValueError("duplicate name")
        del self._by_name[old_name]
        self._by_name[new_name] = item_id

    def resolve(self, name):
        return self._by_name[name]

    def name_for(self, item_id):
        return self._by_id[item_id]

    def __len__(self):
        return len(self._by_id)
