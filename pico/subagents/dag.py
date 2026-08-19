"""Small deterministic DAG validation and readiness helpers."""

from __future__ import annotations

from .contracts import SubtaskRecord, SubtaskSpec


def _reaches(graph, start, target):
    pending = list(graph.get(start, ()))
    seen = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return False


def validate_graph(records: dict[str, SubtaskRecord], additions: tuple[SubtaskSpec, ...]):
    if len({item.task_id for item in additions}) != len(additions):
        raise ValueError("subtask ids must be unique within one delegation")

    specs = {task_id: record.spec for task_id, record in records.items()}
    for spec in additions:
        existing = specs.get(spec.task_id)
        if existing is not None and existing != spec:
            raise ValueError(f"subtask id already has a different contract: {spec.task_id}")
        specs[spec.task_id] = spec

    for spec in specs.values():
        missing = sorted(set(spec.depends_on) - set(specs))
        if missing:
            raise ValueError(
                f"subtask {spec.task_id} has unknown dependencies: {', '.join(missing)}"
            )

    graph = {task_id: tuple(spec.depends_on) for task_id, spec in specs.items()}
    visiting = set()
    visited = set()

    def visit(task_id):
        if task_id in visiting:
            raise ValueError("subtask dependency graph contains a cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)

    implementations = [spec for spec in specs.values() if spec.kind == "implement"]
    for index, left in enumerate(implementations):
        for right in implementations[index + 1 :]:
            overlap = set(left.allowed_write_paths) & set(right.allowed_write_paths)
            ordered = _reaches(graph, left.task_id, right.task_id) or _reaches(
                graph, right.task_id, left.task_id
            )
            if overlap and not ordered:
                paths = ", ".join(sorted(overlap))
                raise ValueError(
                    f"unordered implement subtasks {left.task_id} and {right.task_id} "
                    f"have overlapping write paths: {paths}"
                )


def implementation_order(
    records: dict[str, SubtaskRecord], roots: tuple[str, ...]
):
    ordered = []
    seen = set()

    def visit(task_id):
        if task_id not in records:
            raise ValueError(f"unknown subtask: {task_id}")
        record = records[task_id]
        for dependency in record.spec.depends_on:
            visit(dependency)
        if record.spec.kind == "implement" and task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)

    for task_id in roots:
        visit(task_id)
    return tuple(ordered)


def ready_task_ids(
    records: dict[str, SubtaskRecord],
    target_ids: set[str],
    *,
    completed_ids: set[str],
    failed_ids: set[str],
):
    ready = []
    for task_id in sorted(target_ids):
        record = records[task_id]
        if record.status != "pending":
            continue
        if any(item in failed_ids for item in record.spec.depends_on):
            record.status = "blocked"
            record.error = "dependency failed or was blocked"
            continue
        if all(item in completed_ids for item in record.spec.depends_on):
            ready.append(task_id)
    return ready
