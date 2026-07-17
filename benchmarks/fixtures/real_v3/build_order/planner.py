def build_order(tasks):
    """Return task names in build order."""
    result = []
    visiting = set()

    def visit(name):
        if name in result:
            return
        if name in visiting:
            raise ValueError("dependency cycle")
        visiting.add(name)
        result.append(name)
        for dependency in tasks[name]:
            visit(dependency)
        visiting.remove(name)

    for name in tasks:
        visit(name)
    return result
