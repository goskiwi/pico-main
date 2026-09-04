"""Minimal persisted receipt for the final net workspace Diff."""

import re
from dataclasses import dataclass

from .evidence import WorkspaceDriftError

FINAL_DIFF_ARTIFACT_ID = re.compile(r"^diff_[a-f0-9]{16}_[a-f0-9]{10}$")


@dataclass(frozen=True)
class FinalDiff:
    artifact_id: str = ""
    size_bytes: int = 0

    def __post_init__(self):
        artifact_id = str(self.artifact_id)
        size = int(self.size_bytes)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "size_bytes", size)
        if size < 0:
            raise ValueError("final Diff size cannot be negative")
        if bool(artifact_id) != bool(size):
            raise ValueError("final Diff descriptor fields are inconsistent")
        if artifact_id and not FINAL_DIFF_ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid final Diff artifact id")
    @classmethod
    def from_dict(cls, value):
        expected = {"artifact_id", "size_bytes"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid final Diff descriptor fields")
        return cls(
            artifact_id=value["artifact_id"],
            size_bytes=value["size_bytes"],
        )

    def to_dict(self):
        return {
            "artifact_id": self.artifact_id,
            "size_bytes": self.size_bytes,
        }


def build_final_diff(runtime):
    """Persist the current projection's deterministic net Diff, if any."""

    projection = runtime.run.projection
    diff_text = projection.evidence.change_set.render_final_diff(
        runtime.workspace.root,
        runtime.dependencies.artifacts,
        projection.run_id,
    )
    if not diff_text:
        return FinalDiff()
    descriptor = runtime.dependencies.artifacts.write_final_diff(
        projection.run_id,
        diff_text,
    )
    return FinalDiff(
        artifact_id=descriptor["artifact_id"],
        size_bytes=descriptor["size_bytes"],
    )


def build_stopped_final_diff(runtime):
    try:
        return build_final_diff(runtime)
    except WorkspaceDriftError:
        return None
