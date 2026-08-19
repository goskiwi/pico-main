"""Bounded parent/child orchestration for Pico."""

from .contracts import SubtaskRecord, SubtaskSpec
from .manager import SubagentManager

__all__ = ["SubagentManager", "SubtaskRecord", "SubtaskSpec"]
