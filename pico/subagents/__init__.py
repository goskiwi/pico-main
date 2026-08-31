"""Synchronous parent/child orchestration for Pico."""

from .contracts import ChildRecord, ChildSpec
from .runner import SubagentRunner

__all__ = ["ChildRecord", "ChildSpec", "SubagentRunner"]
