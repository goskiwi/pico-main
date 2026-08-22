"""Project-local configuration helpers."""

import os
import re
from pathlib import Path

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_quotes(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        raise ValueError("invalid .env syntax")
    name, value = line.split("=", 1)
    name = name.strip()
    if not ENV_KEY_PATTERN.match(name):
        raise ValueError(f"invalid .env variable name: {name}")
    return name, _strip_quotes(value)


def find_project_env(start, *, boundary=None):
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    boundary = Path(boundary).resolve() if boundary is not None else None
    if boundary is not None:
        try:
            current.relative_to(boundary)
        except ValueError as exc:
            raise ValueError("project env search starts outside its boundary") from exc
    search_paths = []
    for path in (current, *current.parents):
        search_paths.append(path)
        if boundary is not None and path == boundary:
            break
    for path in search_paths:
        for name in (".env.local", ".env"):
            env_path = path / name
            if env_path.is_symlink():
                raise ValueError(f"{name} must not be a symlink")
            if env_path.is_file():
                return env_path
    return None


def load_project_env(start, *, boundary=None, override=False):
    selected = find_project_env(start, boundary=boundary)
    if selected is None:
        return {}
    env_paths = [selected]
    sibling = selected.with_name(".env")
    local = selected.with_name(".env.local")
    if sibling.is_file() and local.is_file():
        env_paths = [sibling, local]
    loaded = {}
    for env_path in env_paths:
        if env_path.is_symlink():
            raise ValueError(f"{env_path.name} must not be a symlink")
        for number, line in enumerate(
            env_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            try:
                parsed = _parse_env_line(line)
            except ValueError as exc:
                raise ValueError(
                    f"{exc} at {env_path.name}:{number}"
                ) from exc
            if parsed is None:
                continue
            name, value = parsed
            loaded[name] = value
    for name, value in loaded.items():
        if override or name not in os.environ:
            os.environ[name] = value
    return loaded


def provider_env(name, default=""):
    return os.environ.get(name) or default
