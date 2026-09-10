"""Minimal persisted receipt for the final net workspace Diff."""

import re
from dataclasses import dataclass

from .evidence import WorkspaceDriftError
from .workspace import normalize_relative_file

FINAL_DIFF_ARTIFACT_ID = re.compile(r"^diff_[a-f0-9]{16}_[a-f0-9]{10}$")


@dataclass(frozen=True)
class FinalDiff:
    artifact_id: str = ""
    size_bytes: int = 0
    external_paths: tuple[str, ...] = ()

    def __post_init__(self):
        artifact_id = str(self.artifact_id)
        size = int(self.size_bytes)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "size_bytes", size)
        object.__setattr__(self, "external_paths", tuple(sorted({normalize_relative_file(p) for p in self.external_paths})))
        if size < 0:
            raise ValueError("final Diff size cannot be negative")
        if bool(artifact_id) != bool(size):
            raise ValueError("final Diff descriptor fields are inconsistent")
        if artifact_id and not FINAL_DIFF_ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid final Diff artifact id")
    @classmethod
    def from_dict(cls, value):
        expected = {"artifact_id", "size_bytes", "external_paths"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid final Diff descriptor fields")
        if not isinstance(value["external_paths"], list):
            raise TypeError("final Diff external_paths must be a list")
        return cls(
            artifact_id=value["artifact_id"],
            size_bytes=value["size_bytes"],
            external_paths=tuple(value["external_paths"]),
        )

    def to_dict(self):
        return {
            "artifact_id": self.artifact_id,
            "size_bytes": self.size_bytes,
            "external_paths": list(self.external_paths),
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
    external_paths = projection.evidence.external_paths
    if external_paths:
        diff_text = ("Tracked-file workspace delta; includes observed external changes, not solely Agent-authored: "
                     + ", ".join(external_paths) + "\n\n" + diff_text)
    descriptor = runtime.dependencies.artifacts.write_final_diff(
        projection.run_id,
        diff_text,
    )
    return FinalDiff(
        artifact_id=descriptor["artifact_id"],
        size_bytes=descriptor["size_bytes"],
        external_paths=external_paths,
    )


def build_stopped_final_diff(runtime):
    try:
        return build_final_diff(runtime)
    except WorkspaceDriftError:
        return None
