"""Narrow model protocol adapter."""

from .clients import (
    FakeModelClient,
    OpenAICompatibleModelClient,
    ProviderContextOverflow,
)

__all__ = [
    "FakeModelClient",
    "OpenAICompatibleModelClient",
    "ProviderContextOverflow",
]
