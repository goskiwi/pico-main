"""Narrow model protocol adapter."""

from .clients import (
    OpenAICompatibleModelClient,
    ProviderContextOverflow,
)

__all__ = [
    "OpenAICompatibleModelClient",
    "ProviderContextOverflow",
]
