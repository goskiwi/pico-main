"""Narrow model protocol adapter."""

from .clients import FakeModelClient, OpenAICompatibleModelClient

__all__ = [
    "FakeModelClient",
    "OpenAICompatibleModelClient",
]
