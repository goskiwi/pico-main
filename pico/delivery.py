"""Minimal persisted receipt for the final net workspace Diff."""

import re
from dataclasses import dataclass

from .evidence import WorkspaceDriftError

FINAL_DIFF_ARTIFACT_ID = re.compile(r"^diff_[a-f0-9]{16}_[a-f0-9]{10}$")


@dataclass(frozen=True)
class FinalDiffDescriptor:
    diff_artifact_id: str = ""
    diff_bytes: int = 0
    unavailable_reason: str = ""

    def __post_init__(self):
        artifact_id = str(self.diff_artifact_id)
        size = int(self.diff_bytes)
        unavailable_reason = str(self.unavailable_reason).strip()
        object.__setattr__(self, "diff_artifact_id", artifact_id)
        object.__setattr__(self, "diff_bytes", size)
        object.__setattr__(self, "unavailable_reason", unavailable_reason)
        if size < 0:
            raise ValueError("final Diff size cannot be negative")
        if bool(artifact_id) != bool(size):
            raise ValueError("final Diff descriptor fields are inconsistent")
        if artifact_id and not FINAL_DIFF_ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid final Diff artifact id")
        if unavailable_reason and (artifact_id or size):
            raise ValueError(
                "unavailable final Diff cannot contain an artifact receipt"
            )

    @classmethod
    def from_dict(cls, value):
        expected = {"diff_artifact_id", "diff_bytes", "unavailable_reason"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid final Diff descriptor fields")
        return cls(
            diff_artifact_id=value["diff_artifact_id"],
            diff_bytes=value["diff_bytes"],
            unavailable_reason=value["unavailable_reason"],
        )

    def to_dict(self):
        return {
            "diff_artifact_id": self.diff_artifact_id,
            "diff_bytes": self.diff_bytes,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def unavailable(cls, reason):
        reason = str(reason).strip()
        if not reason:
            raise ValueError("unavailable final Diff requires a reason")
        return cls(unavailable_reason=reason)


def build_final_diff_descriptor(runtime):
    """Persist the current projection's deterministic net Diff, if any."""

    projection = runtime.run.projection
    diff_text = projection.evidence.change_set.render_final_diff(
        runtime.workspace.root,
        runtime.dependencies.artifacts,
        projection.run_id,
    )
    if not diff_text:
        return FinalDiffDescriptor()
    descriptor = runtime.dependencies.artifacts.write_final_diff(
        projection.run_id,
        diff_text,
    )
    return FinalDiffDescriptor(
        diff_artifact_id=descriptor["artifact_id"],
        diff_bytes=descriptor["size_bytes"],
    )


def build_stopped_final_diff_descriptor(runtime):
    try:
        return build_final_diff_descriptor(runtime)
    except WorkspaceDriftError:
        return FinalDiffDescriptor.unavailable("workspace_drift")
