"""Security helpers for env filtering and artifact redaction."""

import os

from .config import REDACTED_VALUE, SENSITIVE_ENV_NAME_MARKERS


def looks_sensitive_env_name(name):
    upper = str(name).upper()
    return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)


def is_secret_env_name(agent, name):
    upper = str(name).upper()
    return upper in agent.secret_env_names or looks_sensitive_env_name(upper)


def configured_secret_env_items(agent):
    items = [
        (name, value)
        for name, value in os.environ.items()
        if str(name).upper() in agent.secret_env_names and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def detected_secret_env_items(agent):
    items = [
        (name, value)
        for name, value in os.environ.items()
        if is_secret_env_name(agent, name) and value
    ]
    items.sort(key=lambda item: item[0])
    return items


def secret_env_summary(agent):
    names = [name for name, _ in configured_secret_env_items(agent)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def detected_secret_env_summary(agent):
    names = [name for name, _ in detected_secret_env_items(agent)]
    return {
        "secret_env_count": len(names),
        "secret_env_names": names,
    }


def redact_text(agent, text):
    text = str(text)
    for _, value in sorted(detected_secret_env_items(agent), key=lambda item: len(item[1]), reverse=True):
        text = text.replace(value, REDACTED_VALUE)
    return text


def redact_artifact(agent, value, key=None):
    if key and is_secret_env_name(agent, key):
        return REDACTED_VALUE
    if isinstance(value, dict):
        return {
            str(item_key): redact_artifact(agent, item_value, key=item_key)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_artifact(agent, item, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact_artifact(agent, item, key=key) for item in value]
    if isinstance(value, str):
        return redact_text(agent, value)
    return value


def shell_env(agent):
    env = {
        name: os.environ[name]
        for name in agent.shell_env_allowlist
        if name in os.environ
    }
    env["PWD"] = str(agent.root)
    if "PATH" not in env and os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    return env
